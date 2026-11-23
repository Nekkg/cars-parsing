from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from ads.models import Advertisement
from bot.services import tracking_service as service


class EqualTimestampNotificationTests(TestCase):
    def test_existing_ad_with_identical_clock_timestamp_is_excluded(self):
        stamp = timezone.now()
        def car(key):
            return Advertisement.objects.create(ad_id=key, platform='drom', brand='BMW', model='X5',
                name='BMW X5', price=900000, city='Самара', link='https://auto.drom.ru/bmw/x5/12345678.html')
        with patch('django.utils.timezone.now', return_value=stamp):
            old = car('known')
            row = service.follow(777, 777, {'brand':['bmw']}, 5)
            row.last_check_at = stamp
            row.save()
            new = car('fresh')
            self.assertEqual(old.created_at, new.created_at)
            self.assertEqual(service.queue_new_ads(), 1)
            self.assertEqual(row.start_ad_id, old.pk)
