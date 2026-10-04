from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from ads.models import Advertisement
from ads.search import enqueue, matches, results


class SharedSearchTests(TestCase):
    def test_second_user_does_not_restart_in_flight_search(self):
        first = enqueue({'platform': ['avito']})
        second = enqueue({'platform': ['avito']})
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.requested_at, second.requested_at)

    def test_finished_fresh_search_is_reused(self):
        job = enqueue({'platform': ['avito']})
        job.source_state = {'avito': {'requested_at': job.requested_at.isoformat()}}
        job.save(update_fields=['source_state'])
        self.assertEqual(enqueue(job.filters).requested_at, job.requested_at)

    def test_stale_finished_search_refreshes_once_for_all_users(self):
        job = enqueue({'platform': ['avito']})
        job.requested_at = timezone.now()-timedelta(minutes=10)
        job.source_state = {'avito': {'requested_at': job.requested_at.isoformat()}}
        job.save(update_fields=['requested_at', 'source_state'])
        first = enqueue(job.filters)
        self.assertGreater(first.requested_at, job.requested_at)
        self.assertEqual(enqueue(job.filters).requested_at, first.requested_at)

    def test_indexed_vehicle_keys_keep_aliases_and_skip_other_makes(self):
        selected = Advertisement.objects.create(ad_id='avito:123', platform='avito',
            name='BMW 3', brand='BMW', model='3_SERIES', price=1000000)
        Advertisement.objects.create(ad_id='drom:456', platform='drom',
            name='Audi A4', brand='Audi', model='A4', price=1000000)
        filters = {'vehicle': {'make': {'name': 'BMW', 'slug': 'bmw'},
                               'model': {'name': '3-series', 'slug': '3_series'}}}
        with patch('ads.search.matches', wraps=matches) as matcher:
            self.assertEqual(results(filters), [selected])
            self.assertEqual(matcher.call_count, 1)
