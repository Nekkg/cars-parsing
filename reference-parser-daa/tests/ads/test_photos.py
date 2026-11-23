"""Real image decoding/cache behavior, source URL boundaries and HTTP responses."""
from io import BytesIO
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.error import URLError, HTTPError
from urllib.request import Request
from email.message import Message
from PIL import Image
from django.core.cache import cache
from django.test import TestCase, SimpleTestCase
from ads.models import Advertisement
from ads.photo_cache import cached_photo, fetch_photo, PhotoRedirects, candidates

URL='https://avatars.avto.ru/get-autoru-vos/123/car/1200x900'


def jpeg():
    stream=BytesIO();Image.new('RGB',(1600,1200),'#ab5322').save(stream,'JPEG');return stream.getvalue()


class CacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch=patch('ads.photo_cache.runtime_directory',return_value=Path(self.directory.name))
        self.patch.start();self.addCleanup(self.patch.stop)

    def test_card_is_resized_and_second_read_does_not_download_again(self):
        with patch('ads.photo_cache.fetch_photo',return_value=jpeg()) as fetch:
            path=cached_photo(URL,'autoru','https://auto.ru/car/','card')
            with Image.open(path) as image:self.assertEqual(image.size,(640,480))
            self.assertEqual(cached_photo(URL,'autoru','','card'),path)
            fetch.assert_called_once()

    def test_full_photo_has_a_separate_cache_entry(self):
        with patch('ads.photo_cache.fetch_photo',return_value=jpeg()):
            small=cached_photo(URL,'autoru','','card')
            large=cached_photo(URL,'autoru','','full')
            self.assertNotEqual(small,large)
            with Image.open(large) as image:self.assertEqual(image.size,(1280,960))

    def test_bad_source_data_is_not_cached_as_a_photo(self):
        with patch('ads.photo_cache.fetch_photo',return_value=b'<html>blocked</html>') as fetch:
            with self.assertRaises(Exception):cached_photo(URL,'autoru','','card')
            with self.assertRaises(TimeoutError):cached_photo(URL,'autoru','','card')
            self.assertEqual(fetch.call_count,1)
            self.assertEqual(list(Path(self.directory.name).glob('*.jpg')),[])

    def test_thumbnail_and_gallery_are_the_same_source_asset(self):
        urls=candidates(URL,'autoru','card')
        self.assertEqual(urls,[URL.replace('1200x900','584x438'),URL.replace('avatars.avto.ru','avatars.mds.yandex.net').replace('1200x900','584x438')])

    def test_redirect_to_an_unrelated_or_local_host_is_refused(self):
        handler=PhotoRedirects('autoru')
        for target in ('https://127.0.0.1/private','https://evil.test/image.jpg','http://avatars.avto.ru/image.jpg'):
            with self.assertRaises(ValueError):handler.redirect_request(Request(URL),None,302,'',{},target)

    def test_blocked_image_does_not_switch_hosts(self):
        with patch('ads.photo_cache.source_opener') as create:
            create.return_value.open.side_effect=HTTPError(URL,403,'Restricted',{},None)
            with self.assertRaises(HTTPError):fetch_photo(URL,'autoru','','card')
            self.assertEqual(create.call_count,1)


class PhotoEndpointTests(TestCase):
    def setUp(self):
        self.ad=Advertisement.objects.create(ad_id='autoru:1234567',platform='autoru',name='Test',price=100,
            brand='BMW',model='X5',city='Самара',link='https://auto.ru/car/',image=URL)
        cache.clear()

    def test_feed_has_local_display_urls_and_keeps_source_urls(self):
        row=self.client.get('/api/feed/').json()['items'][0]
        self.assertEqual(row['photos'],[URL])
        self.assertTrue(row['preview_url'].startswith(f'/photos/{self.ad.pk}/0/card/'))
        self.assertTrue(row['display_photos'][0].startswith(f'/photos/{self.ad.pk}/0/full/'))

    def test_unknown_image_or_size_does_not_start_a_download(self):
        with patch('ads.photo_cache.cached_photo') as download:
            self.assertEqual(self.client.get(f'/photos/{self.ad.pk}/5/card/').status_code,404)
            self.assertEqual(self.client.get(f'/photos/{self.ad.pk}/0/huge/').status_code,404)
            download.assert_not_called()

    def test_endpoint_returns_jpeg_with_browser_cache_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.jpg';path.write_bytes(jpeg())
            with patch('ads.photo_cache.cached_photo',return_value=path):
                response=self.client.get(f'/photos/{self.ad.pk}/0/card/')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response['Content-Type'],'image/jpeg')
                self.assertIn('max-age=86400',response['Cache-Control'])
                self.assertTrue(b''.join(response.streaming_content).startswith(b'\xff\xd8'))
                response.close()

    def test_failure_has_a_short_retry_and_no_browser_cache(self):
        with patch('ads.photo_cache.cached_photo',side_effect=TimeoutError):
            response=self.client.get(f'/photos/{self.ad.pk}/0/card/')
        self.assertEqual(response.status_code,503)
        self.assertEqual(response['Retry-After'],'60')
        self.assertEqual(response['Cache-Control'],'no-store')
