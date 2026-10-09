"""Incremental updates use the shared DB, preserve navigation, and resume deliveries."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import sync_to_async
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from ads.models import Advertisement, CatalogNode, SearchJob, ResultView
from users.models import UserFilters
from bot.handlers import result_browser as browser
from bot import live_results as live
from bot.result_presentation import keyboard


class LiveKeyboardTests(SimpleTestCase):
    def test_follow_is_visible_sort_and_status_can_be_restored(self):
        view=ResultView()
        for list_mode in (False,True):
            buttons=[b for row in keyboard(view,{},0,list_mode=list_mode).inline_keyboard for b in row]
            actions=[b.callback_data.split(':')[2] for b in buttons]
            self.assertFalse({'sort','status'}.intersection(actions))
            self.assertTrue({'refresh','new','source','follow'}.issubset(actions))
            restored=[b.callback_data.split(':')[2] for row in keyboard(view,{},0,advanced=True).inline_keyboard for b in row]
            self.assertTrue({'follow','sort','status'}.issubset(restored))

    def test_notice_numbers_and_russian_label(self):
        for count,noun in ((1,'объявление'),(3,'объявления'),(11,'объявлений'),(29,'объявлений'),(21,'объявление')):
            self.assertEqual(live.notice_text('avito',count),f'➕ Авито: {count} {noun} добавлено в ленту')

    def test_callback_and_publisher_use_the_same_lock(self):
        view=ResultView()
        lock=browser.lock_for(view.pk)
        self.assertIs(lock,browser.lock_for(view.pk.hex))


class LivePublisherTests(TestCase):
    def setUp(self):
        self.make=CatalogNode.objects.create(kind='make',key='bmw',name='BMW',slug='bmw')
        self.filters={'vehicle':{'make':{'key':'bmw','name':'BMW','slug':'bmw'}}}
        self.job=SearchJob.objects.create(fingerprint='shared',filters=self.filters)
        UserFilters.objects.create(user_id=7,vehicle=self.make,search_job=self.job)
        self.bot=AsyncMock()
        self.bot.send_message.return_value=SimpleNamespace(message_id=500)
        self.bot.edit_message_media.return_value=SimpleNamespace(photo=[])
        self.now=timezone.now()
        self.initial=self.make_ad('avito',1)
        self.view=ResultView.objects.create(user_id=7,chat_id=7,message_id=101,search_job=self.job,
            filters=self.filters,result_ids=[self.initial.pk],live_until=self.now+timedelta(hours=1),
            live_state={'seen_ids':[self.initial.pk]},last_photo=self.initial.image)
        self.rows=[(self.initial.pk,'avito')]
        browser._photo_ids.clear();browser._failed_photos.clear()

    def make_ad(self,source,index,**kwargs):
        host={'avito':'img.avito.st','autoru':'avatars.mds.yandex.net','drom':'s1.auto.drom.ru'}[source]
        defaults=dict(ad_id=f'{source}:{index}',platform=source,name=f'BMW {index}',price=1000000,
            brand='BMW',brand_key='bmw',image=f'https://{host}/{index}.jpg',images=[f'https://{host}/{index}.jpg',f'https://{host}/{index}-2.jpg'])
        return Advertisement.objects.create(**{**defaults,**kwargs})

    async def add(self,source,index,**kwargs):
        return await sync_to_async(self.make_ad)(source,index,**kwargs)

    async def publish(self,rows=None,seconds=0,expected_sort=None):
        with patch.object(browser,'download_photo',return_value=b'jpeg'):
            await live.publish_view(self.bot,self.view.pk,rows or self.rows,self.job,
                                    self.now+timedelta(seconds=seconds),expected_sort=expected_sort)
        await self.view.arefresh_from_db()

    async def test_first_late_source_updates_same_card_and_sends_notice(self):
        late=await self.add('drom',2)
        await self.publish([(late.pk,'drom'),*self.rows])
        self.assertEqual(self.view.result_ids,[late.pk,self.initial.pk])
        self.assertEqual(self.view.position,1)
        self.bot.edit_message_caption.assert_awaited_once()
        call=self.bot.edit_message_caption.await_args.kwargs
        self.assertEqual(call['message_id'],101)
        self.assertIn('Все 2',str(call['reply_markup']))
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.args,(7,'➕ Дром: 1 объявление добавлено в ленту'))

    async def test_current_ad_photo_platform_and_list_page_survive(self):
        self.view.platform='avito';self.view.photo_index=1;self.view.list_page=0
        self.view.list_message_id=102
        await self.view.asave()
        late=await self.add('avito',2)
        other=await self.add('drom',3)
        await self.publish([(late.pk,'avito'),(other.pk,'drom'),*self.rows])
        self.assertEqual((self.view.platform,self.view.position,self.view.photo_index),('avito',1,1))
        self.assertEqual(self.view.result_ids[self.view.position+1],self.initial.pk)
        self.bot.edit_message_text.assert_awaited()
        self.assertTrue(any(c.kwargs.get('message_id')==102 for c in self.bot.edit_message_text.await_args_list))

    async def test_restart_and_same_rows_do_not_send_or_edit_again(self):
        late=await self.add('avito',2)
        rows=[*self.rows,(late.pk,'avito')]
        await self.publish(rows)
        self.bot.reset_mock()
        await self.publish(rows,seconds=40)
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_text.assert_not_awaited()
        self.bot.edit_message_caption.assert_not_awaited()

    async def test_more_batches_edit_one_cumulative_notice_and_flush_at_finish(self):
        a=await self.add('avito',2);b=await self.add('avito',3);c=await self.add('avito',4)
        await self.publish([*self.rows,(a.pk,'avito')])
        await self.publish([*self.rows,(a.pk,'avito'),(b.pk,'avito')],seconds=5)
        self.bot.edit_message_text.assert_not_awaited()
        await self.publish([*self.rows,(a.pk,'avito'),(b.pk,'avito')],seconds=31)
        self.bot.send_message.assert_awaited_once()
        self.assertIn('2 объявления',self.bot.edit_message_text.await_args.kwargs['text'])
        token=self.job.requested_at.isoformat()
        self.job.source_state={source:{'requested_at':token} for source in live.LABELS}
        await self.job.asave()
        await self.publish([*self.rows,(a.pk,'avito'),(b.pk,'avito'),(c.pk,'avito')],seconds=35)
        self.assertIn('3 объявления',self.bot.edit_message_text.await_args.kwargs['text'])
        self.assertIsNone(self.view.live_until)

    async def test_known_ad_price_update_is_not_counted_as_new_and_returning_ad_not_repeated(self):
        a=await self.add('avito',2)
        rows=[*self.rows,(a.pk,'avito')]
        await self.publish(rows)
        self.bot.reset_mock()
        await Advertisement.objects.filter(pk=a.pk).aupdate(price=1200000)
        await self.publish(self.rows,seconds=31)
        await self.publish(rows,seconds=62)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.view.live_state['added']['avito'],1)

    async def test_retry_after_retains_pending_render_and_does_not_lose_added_ids(self):
        a=await self.add('avito',2)
        rows=[*self.rows,(a.pk,'avito')]
        self.bot.edit_message_caption.side_effect=TelegramRetryAfter(method=SimpleNamespace(),message='rate limit',retry_after=10)
        await self.publish(rows)
        self.assertTrue(self.view.live_state['render_pending'])
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_caption.side_effect=None
        await self.publish(rows,seconds=11)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.view.live_state['added']['avito'],1)
        self.assertFalse(self.view.live_state['render_pending'])

    async def test_deleted_card_or_blocked_bot_stops_publishing(self):
        a=await self.add('avito',2)
        self.bot.edit_message_caption.side_effect=TelegramBadRequest(method=SimpleNamespace(),message='message to edit not found')
        await self.publish([*self.rows,(a.pk,'avito')])
        self.assertIsNone(self.view.live_until)
        self.bot.send_message.assert_not_awaited()

    async def test_finished_with_no_additions_sends_nothing_and_stops(self):
        token=self.job.requested_at.isoformat()
        self.job.source_state={source:{'requested_at':token} for source in live.LABELS}
        await self.job.asave()
        await self.publish()
        self.assertIsNone(self.view.live_until)
        self.bot.send_message.assert_not_awaited()

    async def test_stale_sort_snapshot_cannot_override_callback(self):
        self.view.sort_order='cheap';await self.view.asave()
        late=await self.add('avito',2)
        await self.publish([*self.rows,(late.pk,'avito')],expected_sort='newest')
        self.assertEqual(self.view.result_ids,[self.initial.pk])
        self.bot.send_message.assert_not_awaited()

    async def test_stopped_view_cannot_notify(self):
        self.view.live_until=None;await self.view.asave()
        late=await self.add('avito',2)
        await self.publish([*self.rows,(late.pk,'avito')])
        self.bot.send_message.assert_not_awaited()

    async def test_new_search_disables_old_stream_and_preserves_card(self):
        message=AsyncMock();message.chat=SimpleNamespace(id=7)
        message.answer_photo.return_value=SimpleNamespace(message_id=102,photo=[])
        with patch.object(browser,'download_photo',return_value=b'jpeg'):
            new=await browser.open_browser(message,7)
        await self.view.arefresh_from_db()
        self.assertIsNone(self.view.live_until)
        self.assertEqual(self.view.message_id,101)
        self.assertTrue(new.live_until)

    async def test_shared_search_computed_once_for_two_users_and_notices_separate(self):
        await UserFilters.objects.acreate(user_id=8,vehicle=self.make,search_job=self.job)
        await ResultView.objects.acreate(user_id=8,chat_id=8,message_id=201,search_job=self.job,filters=self.filters,
            result_ids=[self.initial.pk],live_until=self.now+timedelta(hours=1),live_state={'seen_ids':[self.initial.pk]},last_photo=self.initial.image)
        late=await self.add('drom',2)
        rows=[*self.rows,(late.pk,'drom')]
        with patch.object(live,'shared_rows',AsyncMock(return_value=rows)) as query,patch.object(browser,'download_photo',return_value=b'jpeg'):
            await live.poll_once(self.bot)
        query.assert_awaited_once()
        self.assertEqual({c.args[0] for c in self.bot.send_message.await_args_list},{7,8})
        self.assertEqual({c.kwargs['message_id'] for c in self.bot.edit_message_caption.await_args_list},{101,201})

    async def test_only_matching_rows_reported_not_source_processed_counter(self):
        late=await self.add('drom',2)
        await self.add('drom',3,brand='Audi',brand_key='audi',name='Audi A4')
        self.job.source_state={'drom':{'count':500,'pages':20}};await self.job.asave()
        with patch.object(browser,'download_photo',return_value=b'jpeg'):
            await live.poll_once(self.bot)
        await self.view.arefresh_from_db()
        self.assertEqual(self.view.live_state['added'],{'drom':1})
        self.assertIn(late.pk,self.view.result_ids)
        self.assertEqual(self.bot.send_message.await_args.args[1],'➕ Дром: 1 объявление добавлено в ленту')

    async def test_changed_current_query_is_pruned_and_paused_source_remains_watched(self):
        self.job.source_state={'avito':{'status':'доступ ограничен'}};await self.job.asave()
        self.assertEqual(len(await live.candidates()),1)
        await UserFilters.objects.filter(user_id=7).aupdate(search_job=None)
        self.assertEqual(await live.candidates(),[])
        await self.view.arefresh_from_db()
        self.assertIsNone(self.view.live_until)
    async def test_initial_cached_results_do_not_wait_for_all_three_sources(self):
        from bot.handlers.vehicle_search import run_search_once
        from ads.search_status import selected_sources
        message,state=AsyncMock(),AsyncMock()
        progress=SimpleNamespace(message_id=99)
        message.answer.return_value=progress
        with patch.object(browser,'open_browser',AsyncMock()) as opened,patch('bot.handlers.vehicle_search.asyncio.sleep',AsyncMock()) as wait:
            await run_search_once(message,7,state)
        opened.assert_awaited_once_with(message,7,progress)
        wait.assert_not_awaited()
        filters=await UserFilters.objects.aget(user_id=7)
        job=await SearchJob.objects.aget(pk=filters.search_job_id)
        self.assertEqual(selected_sources(job.filters),['avito','autoru','drom'])
        self.assertEqual(job.source_state,{})

    async def test_empty_initial_card_gets_first_result_in_same_message(self):
        self.view.result_ids=[];self.view.live_state={'seen_ids':[]};self.view.last_photo='placeholder'
        await self.view.asave()
        late=await self.add('drom',2)
        await self.publish([(late.pk,'drom')])
        self.bot.edit_message_media.assert_awaited_once()
        self.assertEqual(self.bot.edit_message_media.await_args.kwargs['message_id'],101)
        self.assertEqual(self.view.position,0)
        self.assertEqual(self.view.live_state['added'],{'drom':1})

    async def test_bot_blocked_by_user_stops_without_retries(self):
        late=await self.add('drom',2)
        self.bot.send_message.side_effect=TelegramForbiddenError(method=SimpleNamespace(),message='bot was blocked by the user')
        await self.publish([*self.rows,(late.pk,'drom')])
        self.assertIsNone(self.view.live_until)
    async def test_gallery_arrives_after_collection_updates_same_card_without_notice(self):
        from parsers.models import MediaRequest
        ad=await self.add('autoru',20,images=[])
        await MediaRequest.objects.acreate(advertisement=ad,next_attempt_at=timezone.now())
        self.view.result_ids=[ad.pk]
        self.view.last_photo=ad.image
        self.view.live_state={'seen_ids':[ad.pk],'gallery_pending_id':ad.pk,'gallery_signature':[ad.pk,[ad.image]]}
        await self.view.asave()
        token=self.job.requested_at.isoformat()
        self.job.source_state={source:{'requested_at':token} for source in live.LABELS}
        await self.job.asave()
        rows=[(ad.pk,'autoru')]
        await self.publish(rows)
        self.assertTrue(self.view.live_until)
        self.bot.reset_mock()
        photos=[ad.image,'https://avatars.avto.ru/second.jpg','https://avatars.avto.ru/third.jpg']
        await Advertisement.objects.filter(pk=ad.pk).aupdate(images=photos)
        await MediaRequest.objects.filter(pk=ad.pk).aupdate(finished_at=timezone.now())
        await self.publish(rows,seconds=5)
        self.bot.edit_message_caption.assert_awaited_once()
        self.assertIn('Фото 1 / 3',str(self.bot.edit_message_caption.await_args.kwargs['reply_markup']))
        self.assertEqual(self.bot.edit_message_caption.await_args.kwargs['message_id'],101)
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(self.view.live_until)

    async def test_finished_collection_keeps_watching_newly_queued_current_gallery(self):
        ad=await self.add('autoru',20,images=[])
        self.view.result_ids=[];self.view.live_state={'seen_ids':[]};self.view.last_photo='placeholder'
        await self.view.asave()
        token=self.job.requested_at.isoformat()
        self.job.source_state={source:{'requested_at':token} for source in live.LABELS}
        await self.job.asave()
        await self.publish([(ad.pk,'autoru')])
        self.assertTrue(self.view.live_until)
        self.assertEqual(self.view.live_state['gallery_pending_id'],ad.pk)
    async def test_reopened_old_card_updates_gallery_without_reviving_search_or_notices(self):
        from parsers.models import MediaRequest
        ad=await self.add('autoru',20,images=[])
        await MediaRequest.objects.acreate(advertisement=ad,next_attempt_at=timezone.now())
        self.view.result_ids=[ad.pk]
        self.view.last_photo=ad.image
        self.view.live_state={'seen_ids':[ad.pk],'gallery_pending_id':ad.pk,'gallery_signature':[ad.pk,[ad.image]]}
        await self.view.asave()
        await UserFilters.objects.filter(user_id=7).aupdate(search_job=None)
        active=await live.candidates()
        self.assertEqual(len(active),1)
        self.assertTrue(active[0].media_only)
        await Advertisement.objects.filter(pk=ad.pk).aupdate(images=[ad.image,'https://avatars.avto.ru/second.jpg'])
        await MediaRequest.objects.filter(pk=ad.pk).aupdate(finished_at=timezone.now())
        await self.add('autoru',21)
        with patch.object(live,'shared_rows',AsyncMock()) as query,patch.object(browser,'download_photo',return_value=b'jpeg'):
            await live.poll_once(self.bot)
        query.assert_not_awaited()
        await self.view.arefresh_from_db()
        self.assertEqual(self.view.result_ids,[ad.pk])
        self.bot.edit_message_caption.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(self.view.live_until)