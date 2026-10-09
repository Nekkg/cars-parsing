"""Auto.ru photo regressions: current CDN, ImageObject and size variants."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from bs4 import BeautifulSoup
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from ads.models import Advertisement
from bot.handlers import result_browser
from bot.result_presentation import pictures
from parsers.fetch_cache import fetch_key
from parsers.media_queue import process_media
from parsers.models import MediaRequest, SourceFetch
from parsers.photos import page_photos, unique_photos, valid_photo
from parsers.sources import extract_listings

PHOTO_A = 'https://avatars.avto.ru/get-autoru-vos/2079642/photo-a/'
PHOTO_B = 'https://avatars.avto.ru/get-autoru-vos/2049300/photo-b/'
AD_LINK = 'https://auto.ru/cars/used/sale/volvo/s60/1124949642-b49feaf8/'


def structured_gallery():
    # Auto.ru publishes multiple ImageObjects for every individual photograph.
    data = {'@type': 'Product', 'image': [
        {'@type': 'ImageObject', 'contentUrl': base + size}
        for base in (PHOTO_A, PHOTO_B) for size in ('320x240', '456x342', '1200x900')]}
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


class AutoRuPhotoTests(SimpleTestCase):
    def test_current_gallery_and_jsonld_produce_two_largest_distinct_photos(self):
        gallery = f'''<div class="CardImageGallery"><div class="ImageGalleryDesktop">
        <img class="ImageGalleryDesktop__image" src="{PHOTO_A}584x438"
        srcset="{PHOTO_A}1200x900 1200w, {PHOTO_A}584x438 584w">
        <img loading="lazy" src="{PHOTO_B}584x438" srcset="{PHOTO_B}1200x900 1200w">
        <div class="ImageGalleryDesktop__preview"><img src="{PHOTO_A}small"><img src="{PHOTO_B}small"></div>
        </div></div><div class="Recommendations"><img src="{PHOTO_A}recommended.jpg"></div>'''
        soup = BeautifulSoup(gallery + structured_gallery(), 'html.parser')
        self.assertEqual(page_photos(soup, 'autoru'), [PHOTO_A + '1200x900', PHOTO_B + '1200x900'])

    def test_jsonld_gallery_without_dom_reads_content_url_and_deduplicates(self):
        soup = BeautifulSoup(structured_gallery(), 'html.parser')
        self.assertEqual(page_photos(soup, 'autoru'), [PHOTO_A + '1200x900', PHOTO_B + '1200x900'])

    def test_missing_imageobject_url_falls_back_to_real_primary(self):
        soup = BeautifulSoup('<script type="application/ld+json">'
            '{"@type":"Product","image":[{"@type":"ImageObject"}]}</script>', 'html.parser')
        self.assertEqual(page_photos(soup, 'autoru', PHOTO_A + '1200x900'), [PHOTO_A + '1200x900'])
        self.assertEqual(page_photos(soup, 'autoru'), [])

    def test_cdn_allowed_without_allowing_foreign_hosts_or_html_homepage(self):
        self.assertEqual(valid_photo('//' + (PHOTO_A + '1200x900').split('://')[1], 'autoru'), PHOTO_A + '1200x900')
        for value in ('', ' ', None, 'https://auto.ru', 'https://auto.ru/',
                'https://avatars.avto.ru.evil.test/a.jpg', 'https://st.avto.ru/autoru/icon.webp',
                'http://avatars.avto.ru/a.jpg', 'https://secret@avatars.avto.ru/a.jpg',
                'https://auto.ru/loading.svg'):
            with self.subTest(value=value):
                self.assertEqual(valid_photo(value, 'autoru'), '')

    def test_legacy_cdn_size_variants_are_also_deduplicated(self):
        base = PHOTO_A.replace('avatars.avto.ru', 'avatars.mds.yandex.net')
        self.assertEqual(unique_photos([base + '320x240', base + '1200x900', PHOTO_B + '832x624'], 'autoru'),
                         [base + '1200x900', PHOTO_B + '832x624'])

    def test_small_thumbnails_before_full_images_keep_order_and_best_resolution(self):
        self.assertEqual(unique_photos([PHOTO_A + 'small', PHOTO_B + 'small', PHOTO_A + '1200x900', PHOTO_B + '832x624'], 'autoru'),
                         [PHOTO_A + '1200x900', PHOTO_B + '832x624'])

    def test_listing_skips_placeholder_and_uses_largest_lazy_car_image(self):
        soup = f'''<div class="ListingItem"><a class="ListingItemTitle__link" href="{AD_LINK}">Volvo S60 2005</a>
        <span class="ListingItemPrice__content">699 999</span><div class="ListingItemThumb">
        <img src="https://auto.ru/loading.svg"><img src="data:image/gif;base64,empty"
        data-src="{PHOTO_A}584x438" data-srcset="{PHOTO_A}1200x900 1200w, {PHOTO_A}584x438 584w">
        </div></div>'''
        rows = extract_listings(soup, 'autoru')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['image'], PHOTO_A + '1200x900')
        self.assertEqual(rows[0]['price'], 699999)
        self.assertEqual(rows[0]['external_id'], '1124949642')

    def test_invalid_saved_gallery_does_not_hide_valid_primary(self):
        ad = SimpleNamespace(platform='autoru', image=PHOTO_A + '1200x900', images=['https://auto.ru'])
        self.assertEqual(pictures(ad), [PHOTO_A + '1200x900'])

    async def test_current_cdn_reaches_telegram_media_delivery(self):
        ad = SimpleNamespace(platform='autoru', image=PHOTO_A + '1200x900', images=[], link=AD_LINK)
        result_browser._photo_ids.clear()
        result_browser._failed_photos.clear()
        with patch.object(result_browser, 'download_photo', return_value=b'jpeg') as download:
            media, key, available = await result_browser.media_for(ad, 0)
        self.assertTrue(available)
        self.assertEqual(key, PHOTO_A + '1200x900')
        self.assertEqual(media.data, b'jpeg')
        download.assert_called_once_with(PHOTO_A + '1200x900', AD_LINK)


class AutoRuGalleryQueueTests(TestCase):
    def test_corrupt_detail_cache_is_refetched_and_shared_gallery_is_saved(self):
        ad = Advertisement.objects.create(ad_id='autoru:1124949642', name='Volvo S60 2005', price=699999,
            platform='autoru', link=AD_LINK, image=PHOTO_A + '1200x900')
        SourceFetch.objects.create(key=fetch_key('autoru', 'detail', AD_LINK), source='autoru',
            expires_at=timezone.now() + timedelta(hours=1), payload={'details': {'image': 'https://auto.ru', 'images': ['https://auto.ru']}})
        async_to_sync(result_browser.request_photos)(ad)
        with patch('parsers.collector.load_page', return_value=structured_gallery()) as load:
            self.assertFalse(process_media(None, 'autoru'))
        load.assert_called_once_with(None, 'autoru', AD_LINK)
        ad.refresh_from_db()
        self.assertEqual(ad.images, [PHOTO_A + '1200x900', PHOTO_B + '1200x900'])
        self.assertEqual(ad.image, PHOTO_A + '1200x900')
        self.assertEqual(ad.price, 699999)
        row = MediaRequest.objects.get(pk=ad.pk)
        self.assertIsNotNone(row.finished_at)
        self.assertEqual(row.error_code, '')
