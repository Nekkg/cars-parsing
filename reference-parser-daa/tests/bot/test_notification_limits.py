from datetime import timedelta
from asgiref.sync import async_to_sync
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from users.models import User, UserFilters
from ads.models import Advertisement, CatalogNode, Delivery
from ads.search import request_search
from ads.notifications import pending_deliveries,notification_allowed
from unittest.mock import AsyncMock,patch


class NotificationLimitsTests(TestCase):
    def setUp(self):
        self.filters=UserFilters.objects.create(user_id=1)
        self.job=request_search(self.filters)
        self.user=User.objects.create(user_id=1,monitoring_enabled=True,
            notifications_started_at=timezone.now(),notification_fingerprint=self.job.fingerprint)
        self.ads=[Advertisement.objects.create(ad_id=str(i),name='BMW',price=500000+i) for i in range(12)]

    def test_background_batch_has_only_three_and_stops_when_disabled(self):
        pairs=pending_deliveries()
        self.assertEqual(len(pairs),3)
        self.assertEqual([ad.pk for _,ad in pairs],[ad.pk for ad in reversed(self.ads[-3:])])
        self.user.monitoring_enabled=False;self.user.save()
        self.assertFalse(notification_allowed(1,pairs[0][1]))
        self.assertEqual(pending_deliveries(),[])

    def test_no_backlog_and_three_per_minute(self):
        Advertisement.objects.update(created_at=self.user.notifications_started_at-timedelta(days=1))
        self.assertEqual(pending_deliveries(),[])
        for ad in self.ads[:3]: Delivery.objects.create(user_id=1,advertisement=ad,price=ad.price)
        fresh=Advertisement.objects.create(ad_id='fresh',name='BMW',price=500000)
        self.assertFalse(notification_allowed(1,fresh))
        self.assertEqual(pending_deliveries(),[])

    def test_model_selection_pauses_subscription_and_does_not_enqueue_search(self):
        from bot.handlers.vehicle_search import select_vehicle
        make=CatalogNode.objects.create(kind='make',key='bmw',slug='bmw',name='BMW')
        async_to_sync(select_vehicle)(1,make.pk)
        self.user.refresh_from_db();self.filters.refresh_from_db()
        self.assertFalse(self.user.monitoring_enabled)
        self.assertIsNone(self.filters.search_job_id)
        self.assertEqual(pending_deliveries(),[])


class SingleSearchTests(SimpleTestCase):
    async def test_active_search_is_not_started_twice(self):
        from bot.handlers.vehicle_search import launch_search
        from bot.interactions import active_searches
        active_searches.add(1)
        try:
            with patch('bot.handlers.vehicle_search.run_search_once',AsyncMock()) as run:
                await launch_search(AsyncMock(),1,AsyncMock())
                run.assert_not_awaited()
        finally: active_searches.discard(1)
