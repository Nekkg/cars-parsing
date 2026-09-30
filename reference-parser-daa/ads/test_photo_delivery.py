from io import BytesIO
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from PIL import Image
from bot.photo_delivery import download_photo
from ads.search_status import coverage_text


class PhotoTests(SimpleTestCase):
    def tearDown(self):
        download_photo.cache_clear()

    def test_download_converts_image_to_bounded_jpeg_and_caches(self):
        data=BytesIO()
        Image.new('RGBA',(2400,1800),(50,100,150,200)).save(data,format='PNG')
        response=Mock()
        response.read.return_value=data.getvalue()
        context=Mock()
        context.__enter__=Mock(return_value=response)
        context.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=context
        with patch('bot.photo_delivery.source_opener',return_value=opener):
            photo=download_photo('https://example.com/p.png','https://example.com/car')
            again=download_photo('https://example.com/p.png','https://example.com/car')
        self.assertEqual(photo,again)
        self.assertEqual(opener.open.call_count,1)
        with Image.open(BytesIO(photo)) as image:
            self.assertEqual(image.format,'JPEG')
            self.assertEqual(image.size,(1600,1200))

    def test_invalid_response_is_not_sent_as_photo(self):
        response=Mock();response.read.return_value=b'<html>access denied</html>'
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.return_value=context
        with patch('bot.photo_delivery.source_opener',return_value=opener), self.assertRaises(OSError):
            download_photo('https://example.com/bad')

    def test_blocked_source_is_not_misrepresented_as_zero_matches(self):
        text=coverage_text({'avito':0,'drom':45},{'avito':{'status':'доступ ограничен'}})
        self.assertIn('Avito: доступ ограничен',text)
        self.assertNotIn('Avito: 0',text)
        self.assertIn('Дром: 45',text)
