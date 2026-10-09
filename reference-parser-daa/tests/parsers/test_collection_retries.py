"""Access blocks, local deadlines and source outages require different retries."""
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from ads.models import SearchJob
from parsers.collector import collection_error_state, saved_backoff_seconds, collect
from parsers.sources import SourceBlocked, SourceTemporaryError, SourceUnavailable


class CollectionFailureTests(SimpleTestCase):
    def test_temporary_failure_retries_only_the_search_and_is_bounded(self):
        token = timezone.now().isoformat()
        previous = {}
        for attempt in range(1, 5):
            previous = collection_error_state(SourceUnavailable('avito', 'captcha', 500), token, previous)
            self.assertEqual(previous['network_failures'], attempt)
            self.assertEqual(previous['error_code'], 'network')
            self.assertEqual(previous['requested_at'], None if attempt <= 3 else token)
            self.assertEqual(bool(previous['retry_after']), attempt <= 3)

    def test_local_deadline_is_not_a_fifteen_minute_access_block(self):
        token = timezone.now().isoformat()
        now = timezone.now()
        with patch('parsers.collector.timezone.now', return_value=now):
            state = collection_error_state(SourceTemporaryError('avito: request time budget exceeded'), token)
        self.assertEqual(state['retry_after'], (now + timedelta(seconds=30)).isoformat())
        self.assertEqual(state['error_code'], 'network')

    def test_true_access_restriction_keeps_source_cooldown(self):
        now = timezone.now()
        with patch('parsers.collector.timezone.now', return_value=now):
            state = collection_error_state(SourceBlocked('avito: HTTP 403'), now.isoformat())
        self.assertEqual(state['retry_after'], (now + timedelta(minutes=15)).isoformat())
        self.assertEqual(state['error_code'], 'blocked')


class CollectionRetryIntegrationTests(TestCase):
    def test_network_retry_is_not_restored_as_a_global_access_pause(self):
        SearchJob.objects.create(fingerprint='network-retry', source_state={'avito':{
            'error_code':'network','retry_after':(timezone.now()+timedelta(seconds=120)).isoformat()}})
        self.assertEqual(saved_backoff_seconds('avito'), 0)

    def test_temporary_detail_failure_keeps_unfinished_queue(self):
        from tests.parsers.test_generation_results import vehicle_catalog, card
        _, filters = vehicle_catalog()
        ad = card(identifier='123456789')
        ad['link'] = 'https://www.avito.ru/moskva/avtomobili/volvo_s60_123456789'
        job = SearchJob.objects.create(fingerprint='retry-details', filters=filters)
        job.source_state = {'avito':{'cycle':job.requested_at.isoformat(),'route':0,'next_page':2,
            'pages':1,'count':1,'listing_finished':False,'detail_queue':[ad],'query_years':[2000,2004],'query_years_version':2}}
        job.save(update_fields=['source_state'])
        with patch('parsers.collector.fetch_ad_details', side_effect=SourceUnavailable('avito','page',503)):
            with self.assertRaises(SourceTemporaryError):
                collect(None, 'avito', job)
        job.refresh_from_db()
        self.assertEqual(len(job.source_state['avito']['detail_queue']), 1)
        self.assertEqual(job.source_state['avito']['next_page'], 2)
