"""Saved tracking is multi-query, owner-scoped, scheduled and restart-safe."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import sync_to_async
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import BufferedInputFile
from django.test import TestCase
from django.utils import timezone
from ads.models import Advertisement, SearchJob, CatalogNode, ResultView
from ads.search import enqueue
from bot import tracking
from bot.handlers import tracking as handler
from bot.services import tracking_service as service
from parsers.collector import active_search_jobs, collect
from users.models import TrackedSearch, TrackingDelivery, UserFilters

BMW = {'vehicle': {'make': {'name': 'BMW', 'slug': 'bmw', 'key': 'bmw'}}, 'price': '500000-'}
VOLVO = {'vehicle': {'make': {'name': 'Volvo', 'slug': 'volvo', 'key': 'volvo'}}}


def make_ad(key, brand='BMW', **extra):
    values = dict(ad_id=key, platform='drom', name=brand+' X5', brand=brand, model='X5',
                  price=900000, year='2018', city='Самара', link='https://auto.drom.ru/bmw/x5/12345678.html')
    values.update(extra)
    return Advertisement.objects.create(**values)


def wake(row):
    row.last_check_at=timezone.now()
    row.save(update_fields=['last_check_at'])
    return row


class SavedTrackingTests(TestCase):
    def test_multiple_queries_do_not_modify_current_filter_and_survive_a_new_search(self):
        make=CatalogNode.objects.create(kind='make',key='volvo',slug='volvo',name='Volvo')
        current=UserFilters.objects.create(user_id=1,platform=['avito'],year='2000-2004')
        bmw=service.follow(1,1,BMW,2)
        volvo=service.follow(1,1,VOLVO,10)
        from bot.handlers.vehicle_search import select_vehicle
        from asgiref.sync import async_to_sync
        async_to_sync(select_vehicle)(1,make.pk)
        current.refresh_from_db();bmw.refresh_from_db();volvo.refresh_from_db()
        self.assertEqual(current.platform,['avito'])
        self.assertEqual(current.year,'2000-2004')
        self.assertTrue(bmw.enabled and volvo.enabled)
        self.assertEqual(bmw.filters,BMW)
        self.assertEqual(TrackedSearch.objects.filter(user_id=1).count(),2)

    def test_same_query_is_updated_once_and_users_share_a_periodic_job(self):
        a=service.follow(1,1,BMW,1)
        same=service.follow(1,1,BMW,30)
        b=service.follow(2,2,BMW,5)
        manual=enqueue(BMW)
        self.assertEqual(a.pk,same.pk)
        self.assertEqual(a.search_job_id,b.search_job_id)
        self.assertNotEqual(manual.pk,a.search_job_id)
        self.assertEqual(a.search_job.kind,'tracking')
        self.assertEqual(TrackedSearch.objects.count(),2)
        self.assertEqual(same.interval_minutes,30)

    def test_frequency_change_keeps_new_ad_cutoff_and_pause_resume_skips_pause_backlog(self):
        row=wake(service.follow(1,1,BMW,5))
        initial=row.started_at
        updated=service.change_interval(row.pk,1,1,2)
        self.assertEqual(updated.started_at,initial)
        service.set_enabled(row.pk,1,1,False)
        make_ad('during-pause')
        resumed=service.set_enabled(row.pk,1,1,True)
        wake(resumed)
        self.assertGreater(resumed.started_at,initial)
        self.assertEqual(service.queue_new_ads(),0)
        make_ad('after-resume')
        self.assertEqual(service.queue_new_ads(),1)

    def test_foreign_user_or_chat_cannot_change_or_delete_saved_query(self):
        row=service.follow(1,1,BMW,5)
        self.assertIsNone(service.change_interval(row.pk,2,1,1))
        self.assertIsNone(service.set_enabled(row.pk,1,2,False))
        self.assertFalse(service.remove(row.pk,2,1))
        row.refresh_from_db()
        self.assertTrue(row.enabled)
        self.assertEqual(row.interval_minutes,5)

    def test_invalid_frequency_and_unfiltered_watch_are_rejected(self):
        for value in (0,3,-1,60):
            with self.assertRaises(ValueError):service.follow(1,1,BMW,value)
        with self.assertRaises(ValueError):service.follow(1,1,{},5)
        self.assertEqual(TrackedSearch.objects.count(),0)

    def test_saved_query_is_deep_copied_and_tab_is_captured(self):
        view=ResultView(user_id=1,chat_id=1,filters=deepcopy(BMW),platform='drom')
        data=service.view_query(view)
        service.follow(1,1,data,5)
        data['vehicle']['make']['name']='changed'
        view.platform='autoru'
        row=TrackedSearch.objects.get()
        self.assertEqual(row.filters['platform'],['drom'])
        self.assertEqual(row.filters['vehicle']['make']['name'],'BMW')
        self.assertNotIn('platform',view.filters)

    def test_active_job_waits_for_first_due_check_and_stops_on_pause(self):
        row=service.follow(1,1,BMW,5)
        self.assertNotIn(row.search_job_id,list(active_search_jobs().values_list('pk',flat=True)))
        wake(row)
        self.assertIn(row.search_job_id,list(active_search_jobs().values_list('pk',flat=True)))
        service.set_enabled(row.pk,1,1,False)
        self.assertNotIn(row.search_job_id,list(active_search_jobs().values_list('pk',flat=True)))


class TrackingSchedulerTests(TestCase):
    def due(self,user,query=BMW,minutes=1):
        row=service.follow(user,user,query,minutes)
        row.next_check_at=timezone.now()-timedelta(seconds=1)
        row.save(update_fields=['next_check_at'])
        return row

    def complete(self,row,blocked=False):
        job=SearchJob.objects.get(pk=row.search_job_id)
        token=job.requested_at.isoformat()
        job.source_state={s:{'requested_at':token} for s in ('avito','drom','autoru')}
        if blocked:job.source_state['avito']={'error_code':'blocked','requested_at':None}
        job.save(update_fields=['source_state'])
        return job

    def test_due_users_share_one_cycle_with_individual_next_check_times(self):
        fast=self.due(1,minutes=1);slow=self.due(2,minutes=10)
        manual=enqueue(BMW);manual_time=manual.requested_at
        self.assertEqual(service.schedule_due_checks(),[fast.search_job_id])
        fast.refresh_from_db();slow.refresh_from_db();manual.refresh_from_db()
        self.assertEqual(manual.requested_at,manual_time)
        self.assertAlmostEqual((fast.next_check_at-fast.last_check_at).total_seconds(),60,delta=0.1)
        self.assertAlmostEqual((slow.next_check_at-slow.last_check_at).total_seconds(),600,delta=0.1)
        self.assertEqual(service.schedule_due_checks(),[])

    def test_inflight_cycle_is_not_cancelled_by_another_interval(self):
        row=self.due(1);service.schedule_due_checks()
        job=SearchJob.objects.get(pk=row.search_job_id)
        token=job.requested_at
        row.next_check_at=timezone.now()-timedelta(seconds=1);row.save(update_fields=['next_check_at'])
        self.assertEqual(service.schedule_due_checks(),[])
        job.refresh_from_db();row.refresh_from_db()
        self.assertEqual(job.requested_at,token)
        self.assertGreater(row.next_check_at,timezone.now())

    def test_blocked_avito_does_not_stop_the_other_platforms_next_cycle(self):
        row=self.due(1);service.schedule_due_checks()
        self.complete(row,blocked=True)
        later=timezone.now()+timedelta(minutes=2)
        with patch.object(service.timezone,'now',return_value=later):
            self.assertEqual(service.schedule_due_checks(),[row.search_job_id])
        row.refresh_from_db()
        self.assertEqual(row.last_check_at,later)

    def test_completed_query_rechecks_after_restart_from_persisted_due_time(self):
        row=self.due(1);service.schedule_due_checks();job=self.complete(row)
        first=job.requested_at
        with patch.object(service.timezone,'now',return_value=timezone.now()+timedelta(seconds=61)):
            self.assertEqual(service.schedule_due_checks(),[job.pk])
        job.refresh_from_db()
        self.assertGreater(job.requested_at,first)


class TrackingOutboxTests(TestCase):
    def test_existing_ads_old_publications_and_nonmatching_ads_are_not_a_backlog(self):
        make_ad('already-known')
        row=wake(service.follow(1,1,BMW,5))
        make_ad('old-publication',published_at=row.started_at-timedelta(days=1))
        make_ad('cheap',price=400000)
        make_ad('wrong-brand',brand='Volvo')
        fresh=make_ad('new')
        self.assertEqual(service.queue_new_ads(),1)
        self.assertEqual(TrackingDelivery.objects.get().advertisement_id,fresh.pk)
        self.assertEqual(service.queue_new_ads(),0)

    def test_shared_match_is_evaluated_once_and_overlap_notifies_once_per_user(self):
        wake(service.follow(1,1,BMW,1))
        wake(service.follow(2,2,BMW,5))
        narrower=deepcopy(BMW);narrower['year']='2018-2018'
        wake(service.follow(1,1,narrower,10))
        make_ad('fresh')
        with patch.object(service,'results',wraps=service.results) as matching:
            self.assertEqual(service.queue_new_ads(),2)
        self.assertEqual(matching.call_count,2)
        self.assertEqual(TrackingDelivery.objects.count(),2)

    def test_three_per_minute_including_leases_and_other_users_are_not_starved(self):
        wake(service.follow(1,1,BMW,1));wake(service.follow(2,2,BMW,5))
        for n in range(110):make_ad(str(n))
        service.queue_new_ads()
        claimed=[service.claim_delivery() for _ in range(6)]
        self.assertEqual([row.user_id for row in claimed],[1,1,1,2,2,2])
        self.assertIsNone(service.claim_delivery())
        for row in claimed:service.sent(row.pk,row.lease_until)
        self.assertIsNone(service.claim_delivery())

    def test_success_survives_restart_and_price_change_does_not_repeat_the_ad(self):
        wake(service.follow(1,1,BMW,5));ad=make_ad('fresh')
        service.queue_new_ads();row=service.claim_delivery()
        service.sent(row.pk,row.lease_until)
        ad.price=800000;ad.save(update_fields=['price'])
        self.assertEqual(service.queue_new_ads(),0)
        self.assertIsNone(service.claim_delivery())
        self.assertIsNotNone(TrackingDelivery.objects.get().sent_at)

    def test_retry_is_persisted_and_old_lease_cannot_mark_a_new_claim_sent(self):
        wake(service.follow(1,1,BMW,5));make_ad('fresh')
        service.queue_new_ads();row=service.claim_delivery();old_lease=row.lease_until
        service.retry(row.pk,old_lease,60)
        self.assertIsNone(service.claim_delivery())
        with patch.object(service.timezone,'now',return_value=timezone.now()+timedelta(seconds=61)):
            again=service.claim_delivery()
            service.sent(again.pk,old_lease)
        again.refresh_from_db()
        self.assertIsNone(again.sent_at)
        self.assertEqual(again.attempts,2)

    def test_pausing_removes_unsent_and_deleting_retains_sent_deduplication(self):
        watch=wake(service.follow(1,1,BMW,5));make_ad('fresh')
        service.queue_new_ads();row=service.claim_delivery();service.sent(row.pk,row.lease_until)
        make_ad('pending');service.queue_new_ads()
        service.set_enabled(watch.pk,1,1,False)
        self.assertEqual(TrackingDelivery.objects.filter(sent_at__isnull=True).count(),0)
        service.remove(watch.pk,1,1)
        row.refresh_from_db()
        self.assertIsNone(row.subscription_id)
        self.assertIsNotNone(row.sent_at)

    def test_changed_price_no_longer_matching_is_not_sent(self):
        wake(service.follow(1,1,BMW,5));ad=make_ad('fresh')
        service.queue_new_ads()
        ad.price=300000;ad.save(update_fields=['price'])
        self.assertIsNone(service.claim_delivery())
        self.assertEqual(TrackingDelivery.objects.count(),0)


class TrackingDeliveryTests(TestCase):
    async def prepare(self):
        self.watch=await sync_to_async(service.follow)(1,1,BMW,5)
        await sync_to_async(wake)(self.watch)
        await sync_to_async(make_ad)('fresh')
        await sync_to_async(service.queue_new_ads)()
        self.bot=AsyncMock()
        self.bot.send_photo.return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='telegram-photo')])
        self.photo=patch.object(tracking,'media_for',AsyncMock(return_value=(BufferedInputFile(b'jpeg','car.jpg'),'url',True)))
        self.photo.start();self.addCleanup(self.photo.stop)

    async def test_success_is_recorded_only_after_telegram_and_not_sent_twice(self):
        await self.prepare()
        self.assertTrue(await tracking.deliver_one(self.bot))
        self.assertFalse(await tracking.deliver_one(self.bot))
        self.bot.send_photo.assert_awaited_once()
        row=await TrackingDelivery.objects.aget()
        self.assertIsNotNone(row.sent_at)
        self.assertIn('Новое по запросу',self.bot.send_photo.await_args.kwargs['caption'])

    async def test_expired_telegram_photo_id_is_cleared_and_uploaded_once(self):
        await self.prepare()
        from bot.handlers import result_browser as browser
        browser._photo_ids['url']='expired-file'
        self.bot.send_photo.side_effect=[TelegramBadRequest(method=SendMessage(chat_id=1,text='x'),message='wrong file identifier'),
                                        SimpleNamespace(photo=[SimpleNamespace(file_id='fresh-file')])]
        await tracking.deliver_one(self.bot)
        self.assertEqual(self.bot.send_photo.await_count,2)
        self.assertEqual(browser._photo_ids['url'],'fresh-file')
        self.assertIsNotNone((await TrackingDelivery.objects.aget()).sent_at)
        browser._photo_ids.pop('url',None)

    async def test_retry_after_does_not_mark_delivery_sent(self):
        await self.prepare()
        self.bot.send_photo.side_effect=TelegramRetryAfter(method=SendMessage(chat_id=1,text='x'),message='retry',retry_after=61)
        await tracking.deliver_one(self.bot)
        row=await TrackingDelivery.objects.aget()
        self.assertIsNone(row.sent_at)
        self.assertIsNone(row.lease_until)
        self.assertGreater((row.next_attempt_at-timezone.now()).total_seconds(),60)
        self.assertFalse(await tracking.deliver_one(self.bot))

    async def test_blocking_bot_pauses_all_saved_queries(self):
        await self.prepare()
        await sync_to_async(service.follow)(1,1,VOLVO,10)
        self.bot.send_photo.side_effect=TelegramForbiddenError(method=SendMessage(chat_id=1,text='x'),message='blocked')
        await tracking.deliver_one(self.bot)
        self.assertEqual(await TrackedSearch.objects.filter(enabled=True).acount(),0)
        self.assertEqual(await TrackingDelivery.objects.filter(sent_at__isnull=True).acount(),0)

    async def test_pause_after_claim_prevents_sending(self):
        await self.prepare()
        row=await sync_to_async(service.claim_delivery)()
        await sync_to_async(service.set_enabled)(self.watch.pk,1,1,False)
        with patch.object(service,'claim_delivery',return_value=row):
            await tracking.deliver_one(self.bot)
        self.bot.send_photo.assert_not_awaited();self.bot.send_message.assert_not_awaited()


class TrackingMenuTests(TestCase):
    async def test_find_button_opens_input_without_filters_and_searches_selected_filters(self):
        message=AsyncMock();message.from_user=SimpleNamespace(id=1)
        with patch('bot.handlers.text_search.open_filters',AsyncMock()) as prompt:
            await handler.find_car(message,AsyncMock())
            prompt.assert_awaited_once()
        await UserFilters.objects.acreate(user_id=1,brand=['BMW'])
        with patch('bot.handlers.vehicle_search.launch_search',AsyncMock()) as search:
            await handler.find_car(message,AsyncMock())
            search.assert_awaited_once()

    async def test_foreign_view_or_invalid_interval_cannot_create_watch(self):
        view=await ResultView.objects.acreate(user_id=1,chat_id=1,filters=BMW)
        message=AsyncMock();message.chat=SimpleNamespace(id=1)
        callback=SimpleNamespace(data=f'track:add:{view.pk.hex}:all:5',message=message,from_user=SimpleNamespace(id=2),answer=AsyncMock())
        await handler.tracking_callback(callback,AsyncMock())
        self.assertEqual(await TrackedSearch.objects.acount(),0)
        callback.from_user.id=1;callback.data=f'track:add:{view.pk.hex}:all:3'
        await handler.tracking_callback(callback,AsyncMock())
        self.assertEqual(await TrackedSearch.objects.acount(),0)

    async def test_malformed_callback_does_not_break_the_bot(self):
        message=AsyncMock();message.chat=SimpleNamespace(id=1)
        callback=SimpleNamespace(data='track:add:bad-uuid:all:5',message=message,from_user=SimpleNamespace(id=1),answer=AsyncMock())
        await handler.tracking_callback(callback,AsyncMock())
        message.answer.assert_awaited_once()
        self.assertEqual(await TrackedSearch.objects.acount(),0)

    async def test_interval_prompt_captures_original_source_and_old_query(self):
        view=await ResultView.objects.acreate(user_id=1,chat_id=1,filters=BMW,platform='drom')
        message=AsyncMock();message.chat=SimpleNamespace(id=1)
        await handler.offer_view(message,view)
        buttons=message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0]
        self.assertEqual([button.text for button in buttons],['1 мин','2 мин','5 мин','10 мин','30 мин'])
        view.platform='autoru';await view.asave(update_fields=['platform'])
        callback=SimpleNamespace(data=buttons[0].callback_data,message=message,from_user=SimpleNamespace(id=1),answer=AsyncMock())
        await handler.tracking_callback(callback,AsyncMock())
        saved=await TrackedSearch.objects.aget()
        self.assertEqual(saved.filters['platform'],['drom'])
        self.assertEqual(saved.interval_minutes,1)

    async def test_results_from_tracking_do_not_overwrite_current_search_form(self):
        watch=await sync_to_async(service.follow)(1,1,BMW,5)
        current=await UserFilters.objects.acreate(user_id=1,brand=['Volvo'],year='2000-2004')
        message=AsyncMock();message.chat=SimpleNamespace(id=1)
        message.answer_photo.return_value=SimpleNamespace(message_id=100,photo=[])
        from bot.handlers.result_browser import open_query_browser
        with patch('bot.handlers.result_browser.download_photo',return_value=b'jpeg'):
            view=await open_query_browser(message,1,watch.filters,watch.search_job_id)
        await current.arefresh_from_db()
        self.assertEqual(current.brand,['Volvo'])
        self.assertEqual(view.filters,BMW)


class IncrementalTrackingParserTests(TestCase):
    def pages(self,kind,counts):
        job=SearchJob.objects.create(kind=kind,fingerprint=kind,filters={'brand':['BMW']})
        payloads=[]
        for i,count in enumerate(counts):
            ad={'platform':'drom','external_id':str(12345678+i),'name':'BMW','price':900000,'year':'2018','city':'Самара','brand':'BMW','model':'X5','link':f'https://auto.drom.ru/bmw/x5/{12345678+i}.html'}
            payloads.append(({'ads':[ad],'has_next':True,'saved_count':1,'new_count':count},False))
        with patch('parsers.collector.fetch_listing_page',side_effect=payloads):
            for _ in counts:collect(None,'drom',job)
        job.refresh_from_db()
        return job,job.source_state['drom']

    def test_periodic_query_stops_after_two_known_pages(self):
        job,state=self.pages('tracking',[0,0])
        self.assertEqual(state['requested_at'],job.requested_at.isoformat())
        self.assertEqual(state['pages'],2)

    def test_new_page_resets_known_frontier(self):
        job,state=self.pages('tracking',[0,1,0])
        self.assertNotEqual(state.get('requested_at'),job.requested_at.isoformat())
        self.assertEqual(state['known_pages'],1)

    def test_interactive_search_still_continues_past_known_pages(self):
        job,state=self.pages('search',[0,0])
        self.assertNotEqual(state.get('requested_at'),job.requested_at.isoformat())
