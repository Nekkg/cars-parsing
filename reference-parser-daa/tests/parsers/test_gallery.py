from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from asgiref.sync import async_to_sync
from bs4 import BeautifulSoup
from ads.models import Advertisement
from parsers.models import MediaRequest, SourceFetch
from parsers.fetch_cache import FetchPending, fetch_key
from parsers.media_queue import process_media
from parsers.photos import page_photos, item_photos
from parsers.sources import SourceBlocked
from bot.handlers.result_browser import request_photos


class GalleryExtractionTests(SimpleTestCase):
    def test_main_gallery_only_not_recommendations_or_html_links(self):
        soup = BeautifulSoup('<div data-ftid="bull-gallery"><a href="https://s1.auto.drom.ru/a.jpg"><img src="https://s1.auto.drom.ru/a.jpg"></a><a href="https://auto.drom.ru/bmw/123.html">Car</a><img src="https://s1.auto.drom.ru/b.webp"><img src="https://evil.test/private.jpg"></div><div><img src="https://s1.auto.drom.ru/recommended.jpg"></div>','html.parser')
        self.assertEqual(page_photos(soup,'drom'),['https://s1.auto.drom.ru/a.jpg','https://s1.auto.drom.ru/b.webp'])

    def test_gallery_anchor_itself_is_read(self):
        soup = BeautifulSoup('<a data-ftid="bull-image" href="https://s1.auto.drom.ru/original.jpg"><img src="https://s1.auto.drom.ru/thumb.jpg"></a>','html.parser')
        self.assertEqual(page_photos(soup,'drom')[0],'https://s1.auto.drom.ru/original.jpg')

    def test_avito_largest_photos_from_own_item(self):
        data = {'images':[{'320x240':'https://img.avito.st/a-small.jpg','1280x960':'https://img.avito.st/a.jpg'},
                         {'url':'https://img.avito.st/b.jpg'},'http://localhost/private']}
        self.assertEqual(item_photos(data,'avito'),['https://img.avito.st/a.jpg','https://img.avito.st/b.jpg'])


    def test_same_drom_asset_sizes_are_one_photo_with_best_resolution(self):
        from parsers.photos import unique_photos
        urls=['https://s1.auto.drom.ru/photo/v2/one/gen600.jpg','https://s1.auto.drom.ru/photo/v2/two/gen1200.jpg','https://s1.auto.drom.ru/photo/v2/one/gen1200.jpg']
        self.assertEqual(unique_photos(urls,'drom'),[urls[2],urls[1]])

    def test_private_and_foreign_photo_urls_are_rejected(self):
        from parsers.photos import unique_photos
        values=['http://127.0.0.1/a.jpg','https://localhost/a.jpg','https://s1.auto.drom.ru.evil.test/a.jpg','https://secret@s1.auto.drom.ru/a.jpg','https://[','https://s1.auto.drom.ru/loading.svg']
        self.assertEqual(unique_photos(values,'drom'),[])

class SharedGalleryQueueTests(TestCase):
    def setUp(self):
        self.ad = Advertisement.objects.create(ad_id='drom:12345678',name='BMW X5',price=1000000,
            platform='drom',link='https://auto.drom.ru/bmw/x5/12345678.html',image='https://s1.auto.drom.ru/main.jpg')

    def request(self): return async_to_sync(request_photos)(self.ad)

    def test_two_users_join_one_request_and_it_reuses_existing_collector(self):
        self.request();self.request()
        self.assertEqual(MediaRequest.objects.count(),1)
        with patch('parsers.collector.fetch_ad_details') as fetch:
            self.assertFalse(process_media(None,'drom'))
        details = fetch.call_args.args[2]
        self.assertEqual(details['external_id'],'12345678')
        self.assertTrue(MediaRequest.objects.get(pk=self.ad.pk).finished_at)
        self.assertIn('уже загружены',self.request())
        with patch('parsers.collector.fetch_ad_details') as fetch:
            process_media(None,'drom')
            fetch.assert_not_called()

    def test_real_detail_fetch_saves_gallery_in_advertisement(self):
        self.request()
        html = '<div data-ftid="bull-gallery"><img src="https://s1.auto.drom.ru/main.jpg"><img src="https://s1.auto.drom.ru/second.jpg"></div>'
        with patch('parsers.collector.load_page',return_value=html):
            process_media(None,'drom')
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.images,['https://s1.auto.drom.ru/main.jpg','https://s1.auto.drom.ru/second.jpg'])
        self.assertEqual(Advertisement.objects.count(),1)

    def test_lease_join_does_not_spend_attempt(self):
        self.request()
        with patch('parsers.collector.fetch_ad_details',side_effect=FetchPending):
            process_media(None,'drom')
        row = MediaRequest.objects.get(pk=self.ad.pk)
        self.assertEqual(row.attempts,0)
        self.assertIsNone(row.finished_at)
        self.assertGreater(row.next_attempt_at,timezone.now())

    def test_blocked_retry_is_bounded_and_source_aware(self):
        self.request()
        with patch('parsers.collector.fetch_ad_details',side_effect=SourceBlocked('blocked')):
            self.assertTrue(process_media(None,'drom'))
        row = MediaRequest.objects.get(pk=self.ad.pk)
        self.assertEqual(row.error_code,'blocked')
        self.assertGreater((row.next_attempt_at-timezone.now()).total_seconds(),890)
        row.attempts=2;row.next_attempt_at=timezone.now();row.save()
        with patch('parsers.collector.fetch_ad_details',side_effect=OSError):
            process_media(None,'drom')
        row.refresh_from_db()
        self.assertTrue(row.finished_at)
        self.assertIn('пока не предоставила',self.request())

    def test_no_other_source_or_future_lease_fetch(self):
        self.request()
        with patch('parsers.collector.fetch_ad_details') as fetch:
            process_media(None,'avito')
            fetch.assert_not_called()
    def test_finished_error_can_only_be_retried_after_cooldown(self):
        self.request()
        row = MediaRequest.objects.get(pk=self.ad.pk)
        row.attempts=3;row.finished_at=timezone.now();row.error_code='blocked'
        from datetime import timedelta
        row.next_attempt_at=timezone.now()+timedelta(minutes=15);row.save()
        self.assertIn('пока не предоставила',self.request())
        row.refresh_from_db();self.assertEqual(row.attempts,3)
        row.next_attempt_at=timezone.now()-timedelta(seconds=1);row.save()
        self.assertIn('Загружаю',self.request())
        row.refresh_from_db();self.assertEqual(row.attempts,0)
        self.assertIsNone(row.finished_at)
    def test_legacy_cover_only_cache_is_refetched_once_for_full_gallery(self):
        from datetime import timedelta
        self.request()
        key=fetch_key('drom','detail',self.ad.link)
        SourceFetch.objects.create(key=key,source='drom',expires_at=timezone.now()+timedelta(hours=1),
            payload={'details':{'images':[self.ad.image]}})
        html='<div data-ftid="bull-gallery"><img src="https://s1.auto.drom.ru/main.jpg"><img src="https://s1.auto.drom.ru/second.jpg"></div>'
        with patch('parsers.collector.load_page',return_value=html) as load:
            process_media(None,'drom')
            load.assert_called_once()
        self.ad.refresh_from_db()
        self.assertEqual(len(self.ad.images),2)
        self.assertTrue(SourceFetch.objects.get(pk=key).payload['gallery_checked'])

    def test_verified_one_photo_cache_is_reused_without_extra_network_request(self):
        from datetime import timedelta
        self.request()
        key=fetch_key('drom','detail',self.ad.link)
        SourceFetch.objects.create(key=key,source='drom',expires_at=timezone.now()+timedelta(hours=1),
            payload={'details':{'images':[self.ad.image]},'gallery_checked':True})
        with patch('parsers.collector.load_page') as load:
            process_media(None,'drom')
            load.assert_not_called()
        self.assertTrue(MediaRequest.objects.get(pk=self.ad.pk).finished_at)