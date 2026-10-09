"""The editable browser must never send another card while navigating."""
import re
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import timedelta
from io import BytesIO
from PIL import Image
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from aiogram.exceptions import TelegramBadRequest
from ads.models import Advertisement, CatalogNode, ResultView
from users.models import UserFilters, User
from bot.result_presentation import caption, keyboard, listing, pictures, code
from bot.photo_delivery import placeholder_photo
from bot.handlers import result_browser as browser


class PresentationTests(SimpleTestCase):
    def test_caption_list_limits_html_and_callbacks(self):
        ad = SimpleNamespace(name='<&😀'*150,price=2000000000,year='2020',city='<&😀'*70,
            platform='avito',mileage=300000,engine='x'*300,gearbox='x'*100,body_type='x'*200,drive='полный',
            generation='<&😀'*70,generation_evidence={'status':'resolved'},published_at=timezone.now(),
            created_at=timezone.now(),link='https://avito.ru/avtomobili/car?x=1&y=2',image=None,images=[])
        visible = unescape(re.sub('<[^>]+>','',caption(ad,False,{'vehicle':{'make':{'name':'😀'*200}}})))
        self.assertLessEqual(len(visible.encode('utf-16-le'))//2,1024)
        self.assertNotIn('<&',caption(ad))
        view = ResultView(filters={'vehicle':{'make':{'name':'BMW'},'model':{'name':'X5'}}})
        text = unescape(re.sub('<[^>]+>','',listing(view,[ad]*10,100)))
        self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
        for row in keyboard(view,{'avito':29,'autoru':18,'drom':6},53,10).inline_keyboard:
            for button in row: self.assertLessEqual(len(button.callback_data.encode()),64)

    def test_placeholder_is_valid_photo(self):
        with Image.open(BytesIO(placeholder_photo())) as photo:
            self.assertEqual(photo.format,'JPEG')
            self.assertEqual(photo.size,(960,600))

    def test_gallery_deduplicates_and_rejects_non_http_links(self):
        ad = SimpleNamespace(platform='avito',image='https://img.avito.st/a.jpg',images=['https://img.avito.st/a.jpg','https://img.avito.st/b.jpg','file:///etc/passwd'])
        self.assertEqual(pictures(ad),['https://img.avito.st/a.jpg','https://img.avito.st/b.jpg'])


class ResultBrowserTests(TestCase):
    def setUp(self):
        self.make = CatalogNode.objects.create(kind='make',key='bmw',name='BMW',slug='bmw')
        UserFilters.objects.create(user_id=7,vehicle=self.make,brand=['bmw'])
        self.ads = []
        for index in range(23):
            source = ('avito','autoru','drom')[index%3]
            image_host = {'avito':'img.avito.st','autoru':'avatars.mds.yandex.net','drom':'s1.auto.drom.ru'}[source]
            ad = Advertisement.objects.create(ad_id=f'{source}:{index}',platform=source,name=f'BMW {index}',brand='BMW',brand_key='bmw',
                model='X5',model_key='x5',price=(23-index)*100000,year='2015',city='Самара',
                image=f'https://{image_host}/{index}.jpg',images=[f'https://{image_host}/{index}.jpg',f'https://{image_host}/{index}-2.jpg'],
                link=f'https://{source}.ru/avtomobili/{index}')
            self.ads.append(ad)
        browser._photo_ids.clear();browser._failed_photos.clear()
        self.message = AsyncMock()
        self.message.chat = SimpleNamespace(id=7)
        self.message.message_id = 101
        self.message.answer_photo.return_value = SimpleNamespace(message_id=101,photo=[SimpleNamespace(file_id='cached-file')])
        self.message.answer.return_value = SimpleNamespace(message_id=102)
        self.message.bot.edit_message_media.return_value = SimpleNamespace(photo=[])
        self.message.bot.edit_message_caption.return_value = SimpleNamespace(photo=[])
        self.state = AsyncMock()

    async def open(self):
        with patch.object(browser,'download_photo',return_value=b'jpeg'):
            return await browser.open_browser(self.message,7)

    async def press(self,view,action,arg='',user_id=7,message_id=101):
        self.message.message_id = message_id
        callback = SimpleNamespace(data=code(view,action,arg),message=self.message,bot=self.message.bot,
                                   from_user=SimpleNamespace(id=user_id),answer=AsyncMock())
        with patch.object(browser,'download_photo',return_value=b'jpeg'):
            await browser.browser_callback(callback,self.state)
        await view.arefresh_from_db()
        return callback

    async def test_initial_one_card_then_next_edits_same_message(self):
        view = await self.open()
        self.message.answer_photo.assert_awaited_once()
        self.message.answer.assert_not_awaited()
        await self.press(view,'move',1)
        self.assertEqual(view.position,1)
        self.message.answer_photo.assert_awaited_once()
        self.message.bot.edit_message_media.assert_awaited_once()
        call = self.message.bot.edit_message_media.await_args.kwargs
        self.assertEqual((call['chat_id'],call['message_id']),(7,101))
        self.assertIn('Auto.ru',call['media'].caption)

    async def test_progress_text_is_converted_to_photo_without_new_message(self):
        with patch.object(browser,'download_photo',return_value=b'jpeg'):
            view = await browser.open_browser(self.message,7,SimpleNamespace(message_id=88))
        self.assertEqual(view.message_id,88)
        self.message.answer_photo.assert_not_awaited()
        self.message.bot.edit_message_media.assert_awaited_once()

    async def test_sources_counts_and_source_selection(self):
        view = await self.open()
        ids,counts = await browser.view_rows(view)
        self.assertEqual(counts,{'avito':8,'autoru':8,'drom':7})
        self.assertEqual([await Advertisement.objects.aget(pk=pk) for pk in ids[:3]][0].platform,'avito')
        await self.press(view,'source','drom')
        ids,_ = await browser.view_rows(view)
        self.assertEqual(len(ids),7)
        for pk in ids: self.assertEqual((await Advertisement.objects.aget(pk=pk)).platform,'drom')
        self.assertEqual(view.position,0)
        await self.press(view,'move',-1)
        self.assertEqual(view.position,6)

    async def test_list_ten_rows_and_next_edits_one_text_message(self):
        view = await self.open()
        await self.press(view,'list')
        self.assertEqual(view.list_message_id,102)
        text = self.message.answer.await_args.args[0]
        self.assertEqual(len(re.findall(r'^\d+\. ',text,re.M)),10)
        self.assertIn('1–10 из 23',text)
        await self.press(view,'listpage',1,message_id=102)
        self.assertEqual(view.list_page,1)
        self.message.answer.assert_awaited_once()
        call = self.message.bot.edit_message_text.await_args.kwargs
        self.assertEqual(call['message_id'],102)
        self.assertIn('11–20 из 23',call['text'])
        await self.press(view,'card',message_id=102)
        self.assertEqual(view.position,10)
        self.message.answer_photo.assert_awaited_once()

    async def test_card_button_converts_visible_list_even_when_photo_matches(self):
        view = await self.open()
        await self.press(view,'list')
        await self.press(view,'card',message_id=102)
        self.assertEqual(view.message_id,102)
        self.assertIsNone(view.list_message_id)
        self.assertEqual(self.message.bot.edit_message_media.await_args.kwargs['message_id'],102)
        self.message.bot.edit_message_caption.assert_not_awaited()
        self.message.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=7,message_id=101,reply_markup=None)
        await self.press(view,'move',1,message_id=102)
        self.assertEqual(self.message.bot.edit_message_media.await_args.kwargs['message_id'],102)
        self.message.answer_photo.assert_awaited_once()
        # Another list is a text message, never editMessageText on the promoted photo.
        self.message.answer.return_value=SimpleNamespace(message_id=103)
        await self.press(view,'list',message_id=102)
        self.assertEqual(view.list_message_id,103)
        await self.press(view,'card',message_id=103)
        self.assertEqual(view.message_id,103)
        self.assertIsNone(view.list_message_id)
        self.message.answer_photo.assert_awaited_once()

    async def test_failed_card_conversion_preserves_list_and_old_card_bindings(self):
        view = await self.open()
        await self.press(view,'list')
        self.message.bot.edit_message_media.side_effect=TelegramBadRequest(method=None,message='message cannot be edited')
        await self.press(view,'card',message_id=102)
        self.assertEqual(view.message_id,101)
        self.assertEqual(view.list_message_id,102)
        self.message.bot.edit_message_reply_markup.assert_not_awaited()
        self.message.bot.edit_message_media.side_effect=None
        await self.press(view,'card',message_id=102)
        self.assertEqual(view.message_id,102)

    async def test_return_uses_current_source_list_page_and_first_photo(self):
        view = await self.open()
        await self.press(view,'source','drom')
        await self.press(view,'photo',1)
        await self.press(view,'list')
        await self.press(view,'card',message_id=102)
        self.assertEqual(view.platform,'drom')
        self.assertEqual(view.photo_index,0)
        self.assertEqual(view.position,0)
        self.assertEqual(self.message.bot.edit_message_media.await_args.kwargs['message_id'],102)
        self.assertIn('Дром',self.message.bot.edit_message_media.await_args.kwargs['media'].caption)
    async def test_sorting_is_global_and_reset_position(self):
        view = await self.open()
        await self.press(view,'move',1)
        await self.press(view,'sort','cheap')
        self.assertEqual(view.position,0)
        first = await Advertisement.objects.aget(pk=view.result_ids[0])
        self.assertEqual(first.price,100000)
        await self.press(view,'sort','expensive')
        first = await Advertisement.objects.aget(pk=view.result_ids[0])
        self.assertEqual(first.price,2300000)

    async def test_old_view_keeps_snapshot_after_new_query_and_refresh(self):
        view = await self.open()
        await UserFilters.objects.filter(user_id=7).aupdate(price='999999999-')
        await self.press(view,'refresh')
        self.assertEqual(len(view.result_ids),23)
        self.assertFalse(view.filters.get('price'))

    async def test_gallery_uses_second_image_and_same_message(self):
        view = await self.open()
        await self.press(view,'photo',1)
        self.assertEqual(view.photo_index,1)
        self.message.answer_photo.assert_awaited_once()
        self.message.bot.edit_message_media.assert_awaited_once()
        self.assertIn('Фото 2 / 2',str(self.message.bot.edit_message_media.await_args.kwargs['reply_markup']))

    async def test_another_user_wrong_message_and_expired_view_cannot_edit(self):
        view = await self.open()
        for user_id,msg in ((8,101),(7,999)):
            call = await self.press(view,'move',1,user_id=user_id,message_id=msg)
            self.assertTrue(call.answer.await_args.kwargs.get('show_alert'))
        await ResultView.objects.filter(pk=view.pk).aupdate(created_at=timezone.now()-timedelta(days=8))
        call = await self.press(view,'move',1)
        self.assertTrue(call.answer.await_args.kwargs.get('show_alert'))
        self.message.bot.edit_message_media.assert_not_awaited()
        self.assertEqual(view.position,0)

    async def test_malformed_uuid_is_rejected(self):
        callback = SimpleNamespace(data='rv:not-a-uuid:move:1',message=self.message,from_user=SimpleNamespace(id=7),answer=AsyncMock())
        await browser.browser_callback(callback,self.state)
        callback.answer.assert_awaited_once()
        self.assertTrue(callback.answer.await_args.kwargs['show_alert'])

    async def test_photo_failure_still_gives_editable_card(self):
        with patch.object(browser,'download_photo',side_effect=OSError):
            view = await browser.open_browser(self.message,7)
        self.assertEqual(view.last_photo,'placeholder')
        self.assertIn('Фото сейчас недоступно',self.message.answer_photo.await_args.kwargs['caption'])
        self.assertTrue(self.message.answer_photo.await_args.kwargs['reply_markup'])

    async def test_empty_source_is_editable_and_does_not_send_cards(self):
        view = await self.open()
        await Advertisement.objects.filter(platform='avito').adelete()
        await self.press(view,'source','avito')
        self.assertIn('пока нет',self.message.bot.edit_message_media.await_args.kwargs['media'].caption)
        self.message.answer_photo.assert_awaited_once()
        await self.press(view,'source','drom')
        self.assertIn('Дром',self.message.bot.edit_message_media.await_args.kwargs['media'].caption)

    async def test_same_caption_not_modified_is_not_an_error_message(self):
        view = await self.open()
        self.message.bot.edit_message_caption.side_effect = TelegramBadRequest(method=SimpleNamespace(),message='message is not modified')
        await self.press(view,'source','all')
        self.message.answer.assert_not_awaited()

    async def test_follow_uses_view_query_and_selected_source(self):
        view = await self.open()
        await self.press(view,'source','drom')
        from bot.handlers.tracking import tracking_callback
        from users.models import TrackedSearch
        from bot.services import tracking_service as service
        from asgiref.sync import sync_to_async
        await self.press(view,'follow')
        picker = self.message.answer.await_args.kwargs['reply_markup']
        data = picker.inline_keyboard[0][2].callback_data
        callback = SimpleNamespace(data=data,message=self.message,from_user=SimpleNamespace(id=7),answer=AsyncMock())
        with patch.object(browser,'download_photo',return_value=b'jpeg'):
            await tracking_callback(callback,self.state)
        self.assertTrue(await browser.is_following(view))
        saved = await TrackedSearch.objects.aget(user_id=7)
        self.assertEqual(saved.filters['platform'],['drom'])
        self.assertEqual(saved.interval_minutes,5)
        filters = await UserFilters.objects.aget(user_id=7)
        self.assertEqual(filters.platform,[])
        await sync_to_async(service.set_enabled)(saved.pk,7,7,False)
        self.assertFalse(await browser.is_following(view))

    async def test_new_search_opens_filters(self):
        view = await self.open()
        with patch('bot.handlers.text_search.open_filters',AsyncMock()) as filters:
            await self.press(view,'new')
        filters.assert_awaited_once_with(self.message,self.state,7)
    async def test_refresh_old_query_enters_shared_collection_without_overwriting_new_query(self):
        from asgiref.sync import sync_to_async
        from parsers.collector import active_search_jobs
        from ads.models import SearchJob
        view = await self.open()
        different = await SearchJob.objects.acreate(fingerprint='new-query',filters={'price':'999999999-'})
        await UserFilters.objects.filter(user_id=7).aupdate(search_job=different,price='999999999-')
        await self.press(view,'refresh')
        self.assertTrue(view.search_job_id)
        filters = await UserFilters.objects.aget(user_id=7)
        self.assertEqual(filters.search_job_id,different.pk)
        active = await sync_to_async(lambda:list(active_search_jobs().values_list('pk',flat=True)))()
        self.assertIn(view.search_job_id,active)
        await ResultView.objects.filter(pk=view.pk).aupdate(collect_until=timezone.now()-timedelta(seconds=1))
        active = await sync_to_async(lambda:list(active_search_jobs().values_list('pk',flat=True)))()
        self.assertNotIn(view.search_job_id,active)

    async def test_expired_telegram_photo_is_reuploaded_once(self):
        view = await self.open()
        next_ad = await Advertisement.objects.aget(pk=view.result_ids[1])
        browser._photo_ids[next_ad.image] = 'expired-file-id'
        self.message.bot.edit_message_media.side_effect = [TelegramBadRequest(method=SimpleNamespace(),message='wrong file identifier'),SimpleNamespace(photo=[])]
        await self.press(view,'move',1)
        self.assertEqual(self.message.bot.edit_message_media.await_count,2)
        self.message.answer_photo.assert_awaited_once()
        self.message.answer.assert_not_awaited()
    async def test_open_card_automatically_requests_current_and_two_next_autoru_galleries(self):
        from parsers.models import MediaRequest
        rows=[]
        for index in range(5):
            rows.append(await Advertisement.objects.acreate(ad_id=f'autoru:gallery-{index}',platform='autoru',
                name=f'BMW {index}',brand='bmw',brand_key='bmw',price=1000000,
                image=f'https://avatars.avto.ru/gallery-{index}.jpg',link=f'https://auto.ru/cars/used/sale/bmw/x5/{index}/'))
        view=await ResultView.objects.acreate(user_id=7,chat_id=7,result_ids=[row.pk for row in rows])
        with patch.object(browser,'download_photo',return_value=b'jpeg'):
            await browser.render_card(self.message,view)
            await browser.render_card(self.message,view)
        queued=set([pk async for pk in MediaRequest.objects.values_list('pk',flat=True)])
        self.assertEqual(queued,{row.pk for row in rows[:3]})
        self.assertEqual(view.live_state['gallery_pending_id'],rows[0].pk)
        self.message.answer_photo.assert_awaited_once()

    async def test_completed_gallery_is_not_requested_automatically_again(self):
        from parsers.models import MediaRequest
        ad=next(ad for ad in self.ads if ad.platform=='autoru')
        await Advertisement.objects.filter(pk=ad.pk).aupdate(images=[])
        await MediaRequest.objects.acreate(advertisement=ad,next_attempt_at=timezone.now(),
            finished_at=timezone.now(),attempts=3,error_code='failed')
        pending=await browser.prefetch_photos([ad.pk],0)
        self.assertIsNone(pending)
        row=await MediaRequest.objects.aget(pk=ad.pk)
        self.assertEqual(row.attempts,3)
        self.assertTrue(row.finished_at)