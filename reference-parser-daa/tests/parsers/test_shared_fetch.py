from datetime import timedelta
from unittest.mock import Mock, patch
from django.test import TestCase
from django.utils import timezone
from ads.models import SearchJob, Advertisement
from parsers.models import SourceFetch
from parsers.fetch_cache import cached_fetch, fetch_key, FetchPending
from parsers.sources import SourceBlocked
from parsers.collector import collect
from tests.parsers.test_avito_client import AvitoAdapterTests


class SharedFetchTests(TestCase):
    def test_reuses_normalized_page_for_other_user(self):
        loader = Mock(return_value={'ads': []})
        url = 'https://www.avito.ru/samara/avtomobili?q=BMW&p=1'
        self.assertFalse(cached_fetch('avito', 'listing', url, loader)[1])
        reordered = 'https://www.avito.ru/samara/avtomobili?p=1&q=BMW'
        self.assertTrue(cached_fetch('avito', 'listing', reordered, loader)[1])
        loader.assert_called_once()

    def test_challenge_is_never_cached_as_empty_results(self):
        loader = Mock(side_effect=SourceBlocked('blocked'))
        with self.assertRaises(SourceBlocked):
            cached_fetch('avito', 'listing', 'https://www.avito.ru/', loader)
        row = SourceFetch.objects.get()
        self.assertEqual(row.payload, {})
        self.assertIsNone(row.lease_until)

    def test_other_worker_joins_active_fetch(self):
        url = 'https://www.avito.ru/'
        now = timezone.now()
        SourceFetch.objects.create(key=fetch_key('avito', 'listing', url), source='avito',
            expires_at=now, lease_until=now+timedelta(seconds=30))
        loader = Mock()
        with self.assertRaises(FetchPending):
            cached_fetch('avito', 'listing', url, loader)
        loader.assert_not_called()

    def test_expired_page_is_refetched(self):
        url = 'https://www.avito.ru/'
        loader = Mock(side_effect=[{'ads': [1]}, {'ads': [2]}])
        cached_fetch('avito', 'listing', url, loader)
        SourceFetch.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        payload, reused = cached_fetch('avito', 'listing', url, loader)
        self.assertFalse(reused)
        self.assertEqual(payload['ads'], [2])

    def test_different_budgets_share_one_avito_fetch_and_write(self):
        base = {'platform': ['avito'], 'vehicle': {'make': {'name': 'BMW', 'slug': 'bmw'}}}
        first = SearchJob.objects.create(fingerprint='budget-a', filters={**base, 'price': '1000000-'})
        second = SearchJob.objects.create(fingerprint='budget-b', filters={**base, 'price': '-6000000'})
        fixture = AvitoAdapterTests()
        with patch('parsers.collector.load_page', return_value=fixture.page([fixture.item()])), patch.dict('os.environ', {'AVITO_TRANSPORT': 'http'}), patch('parsers.collector.save_ad', wraps=__import__('parsers.collector', fromlist=['save_ad']).save_ad) as save:
            collect(Mock(), 'avito', first)
            collect(Mock(), 'avito', second)
            self.assertEqual(save.call_count, 1)
        self.assertEqual(SourceFetch.objects.count(), 1)
        self.assertEqual(Advertisement.objects.count(), 1)
