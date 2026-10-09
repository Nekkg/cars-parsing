from io import BytesIO
from ssl import SSLEOFError, SSLCertVerificationError
from urllib.error import URLError, HTTPError
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from PIL import Image
from bot.photo_delivery import download_photo
from ads.search_status import coverage_text


class PhotoTests(SimpleTestCase):
    def tearDown(self):
        download_photo.cache_clear()
        from bot import photo_delivery
        photo_delivery._autoru_cdn_retry_at = 0.0

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

    def test_transient_tls_disconnect_retries_once_and_returns_jpeg(self):
        data=BytesIO()
        Image.new('RGB',(1200,900),'white').save(data,format='JPEG')
        response=Mock();response.read.return_value=data.getvalue()
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.side_effect=[URLError(SSLEOFError('unexpected eof')),context]
        with patch('bot.photo_delivery.source_opener',return_value=opener):
            photo=download_photo('https://avatars.avto.ru/car.jpg')
        self.assertEqual(opener.open.call_count,2)
        with Image.open(BytesIO(photo)) as image:
            self.assertEqual((image.format,image.size),('JPEG',(1200,900)))

    def test_transient_disconnect_has_only_one_retry(self):
        opener=Mock();opener.open.side_effect=URLError(ConnectionResetError('reset'))
        with patch('bot.photo_delivery.source_opener',return_value=opener), self.assertRaises(URLError):
            download_photo('https://avatars.avto.ru/car.jpg')
        self.assertEqual(opener.open.call_count,2)

    def test_access_restriction_and_certificate_errors_are_not_retried(self):
        for error in (HTTPError('https://avatars.avto.ru/car.jpg',403,'forbidden',{},None),
                      URLError(SSLCertVerificationError('invalid certificate'))):
            with self.subTest(error=type(error).__name__):
                opener=Mock();opener.open.side_effect=error
                with patch('bot.photo_delivery.source_opener',return_value=opener), self.assertRaises(URLError):
                    download_photo('https://avatars.avto.ru/car.jpg')
                self.assertEqual(opener.open.call_count,1)

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

    def test_autoru_connection_timeout_uses_same_asset_on_official_alternate_cdn(self):
        from bot import photo_delivery
        data=BytesIO();Image.new('RGB',(1200,900),'white').save(data,format='JPEG')
        response=Mock();response.read.return_value=data.getvalue()
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        opener=Mock();opener.open.side_effect=[URLError(TimeoutError('handshake timeout')),context,context]
        url='https://avatars.avto.ru/get-autoru-vos/123/car/1200x900'
        with patch('bot.photo_delivery.source_opener',return_value=opener):
            result=download_photo(url,'https://auto.ru/cars/1')
            download_photo(url.replace('/car/','/second/'),'https://auto.ru/cars/1')
        calls=[call.args[0].full_url for call in opener.open.call_args_list]
        self.assertEqual(calls,[url,url.replace('avatars.avto.ru','avatars.mds.yandex.net'),
            url.replace('avatars.avto.ru','avatars.mds.yandex.net').replace('/car/','/second/')])
        self.assertGreater(photo_delivery._autoru_cdn_retry_at,0)
        with Image.open(BytesIO(result)) as image:self.assertEqual(image.format,'JPEG')

    def test_official_cdn_fallback_does_not_retry_access_denial_or_bad_certificate(self):
        url='https://avatars.avto.ru/get-autoru-vos/123/car/1200x900'
        for error in (HTTPError(url,403,'forbidden',{},None),URLError(SSLCertVerificationError('invalid certificate'))):
            with self.subTest(error=type(error).__name__):
                opener=Mock();opener.open.side_effect=error
                with patch('bot.photo_delivery.source_opener',return_value=opener),self.assertRaises(URLError):
                    download_photo(url)
                self.assertEqual(opener.open.call_count,1)