"""The initial feed reads existing ads, paginates in SQL and never starts collectors."""
from datetime import timedelta
from unittest.mock import patch
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ads.models import Advertisement, SearchJob, WebSearch
from ads.web_search import stored_snapshot


class StoredFeedTests(TestCase):
    def setUp(self):cache.clear()

    def car(self, platform='avito', **extra):
        index=Advertisement.objects.count()
        data=dict(ad_id=f'{platform}:{index}',platform=platform,name=f'Car {index}',price=1000000+index,
                  city='Самара',brand='BMW',model='X5',link='https://www.avito.ru/samara/avtomobili/car_123456789',
                  image='https://img.avito.st/image/photo.jpg')
        data.update(extra)
        return Advertisement.objects.create(**data)

    def test_anonymous_feed_returns_saved_cars_without_creating_session_or_collection(self):
        car=self.car()
        with patch('ads.views.enqueue',side_effect=AssertionError('Feed started collection')):
            response=self.client.get('/api/feed/')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['items'][0]['id'],car.pk)
            self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(response.json()['mode'],'feed')
        self.assertFalse(response.json()['collecting'])
        self.assertIsNone(response.json()['search_id'])
        self.assertEqual(SearchJob.objects.count(),0)
        self.assertEqual(WebSearch.objects.count(),0)
        self.assertNotIn('sessionid',self.client.cookies)

    def test_page_loads_at_most_eighteen_rows_and_counters_cover_all_sources(self):
        for _ in range(20):self.car()
        other=self.car('drom')
        self.car('unknown');self.car(price=0);self.car(name='')
        with CaptureQueriesContext(connection) as queries:
            first=stored_snapshot()
        self.assertEqual(len(first['items']),18)
        self.assertEqual(first['total_all'],21)
        self.assertEqual(first['counts'],{'avito':20,'autoru':0,'drom':1})
        self.assertEqual(first['pages'],2)
        self.assertEqual(len(queries),2)
        self.assertTrue(any('LIMIT 18' in row['sql'] for row in queries))
        self.assertEqual(len(stored_snapshot(page=2)['items']),3)
        filtered=stored_snapshot(platform='drom')
        self.assertEqual(filtered['total'],1)
        self.assertEqual(filtered['total_all'],21)
        self.assertEqual(filtered['items'][0]['id'],other.pk)

    def test_newest_uses_the_same_freshness_order_as_search_and_price_sort_is_supported(self):
        recent=self.car(price=2000000)
        published=self.car(price=1000000)
        old=self.car(price=3000000)
        Advertisement.objects.filter(pk=published.pk).update(published_at=timezone.now()+timedelta(days=1))
        self.assertEqual(stored_snapshot()['items'][0]['id'],published.pk)
        self.assertEqual(stored_snapshot('cheap')['items'][0]['id'],published.pk)
        self.assertEqual(stored_snapshot('expensive')['items'][0]['id'],old.pk)
        self.assertEqual(stored_snapshot('newest',page=999)['page'],1)

    def test_empty_database_still_returns_a_valid_initial_feed(self):
        data=self.client.get('/api/feed/').json()
        self.assertEqual(data['items'],[])
        self.assertEqual(data['total_all'],0)
        self.assertEqual(data['pages'],1)
        self.assertFalse(data['collecting'])

    def test_feed_reuses_safe_serialization_and_never_exposes_private_payloads(self):
        self.car(link='javascript:alert(1)',image='https://127.0.0.1/private',
                 images=['https://img.avito.st/image/valid.jpg'],generation_evidence={'cookies':'PRIVATE'})
        response=self.client.get('/api/feed/')
        self.assertNotContains(response,'PRIVATE')
        self.assertNotContains(response,'127.0.0.1')
        self.assertEqual(response.json()['items'][0]['url'],'')
        self.assertEqual(response.json()['items'][0]['photos'],['https://img.avito.st/image/valid.jpg'])

    def test_invalid_views_are_rejected_and_feed_is_read_only(self):
        for query in ('sort=invalid','platform=other','page=not-a-number'):
            self.assertEqual(self.client.get('/api/feed/?'+query).status_code,400)
        self.assertEqual(self.client.post('/api/feed/').status_code,405)
