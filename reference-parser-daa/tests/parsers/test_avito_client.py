import json
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from parsers.avito_client import AvitoClient, avito_proxy
from parsers.sources import extract_listings, SourceBlocked, source_url


class AvitoAdapterTests(SimpleTestCase):
    def item(self, identifier=123456789):
        return {'id': identifier, 'title': 'BMW X5, 2020, 45 000 км',
                'urlPath': f'/samara/avtomobili/bmw_x5_{identifier}',
                'priceDetailed': {'value': 5000000}, 'location': {'name': 'Самара'},
                'images': [{'140x105': 'https://img.example/small.jpg',
                            '1280x960': 'https://img.example/large.jpg'}]}

    def page(self, items):
        payload = {'loaderData': {'data': {'catalog': {'items': items}}}}
        return '<script type="mime/invalid" data-mfe-state="true">' + json.dumps(payload) + '</script>'

    def test_structured_catalog_maps_city_photo_and_all_items(self):
        ads = extract_listings(self.page([self.item(123456789+i) for i in range(25)]), 'avito')
        self.assertEqual(len(ads), 25)
        self.assertEqual(ads[0]['city'], 'Самара')
        self.assertEqual(ads[0]['image'], 'https://img.example/large.jpg')
        self.assertEqual(ads[0]['mileage'], 45000)
        self.assertEqual(ads[0]['model'], 'X5')

    def test_widgets_and_duplicate_ids_are_not_ads(self):
        self.assertEqual(len(extract_listings(self.page([{}, self.item(), self.item()]), 'avito')), 1)

    def test_empty_catalog_is_valid_end(self):
        self.assertEqual(extract_listings(self.page([]), 'avito'), [])

    def test_bad_schema_is_not_empty_results(self):
        with self.assertRaises(RuntimeError):
            extract_listings(self.page([{'unexpected': True}]), 'avito')

    def test_block_not_confused_with_embedded_ads(self):
        with self.assertRaises(SourceBlocked):
            extract_listings('<title>Доступ ограничен</title>' + self.page([self.item()]), 'avito')

    def test_http_block_has_no_retry_and_clears_previous_page(self):
        client = AvitoClient.__new__(AvitoClient)
        client.session = Mock()
        client.session.get.return_value.status_code = 429
        client.last_request = 0
        client.page_source = 'previous page'
        with patch.object(client, 'load_cookies'):
            with self.assertRaises(SourceBlocked):
                client.get('https://www.avito.ru/samara/avtomobili')
        client.session.get.assert_called_once()
        self.assertEqual(client.page_source, '')

    def test_route_is_independent_from_other_sources(self):
        with patch.dict('os.environ', {'AVITO_PROXY_URL': '', 'SOURCES_PROXY_URL': 'http://other:3065'}):
            self.assertIsNone(avito_proxy())
        with patch.dict('os.environ', {'AVITO_PROXY_URL': 'http://avito:8000'}):
            self.assertEqual(avito_proxy(), 'http://avito:8000')

    def test_city_and_pagination_are_preserved(self):
        url = source_url('avito', {'city': ['Самара'], 'vehicle': {'make': {'name': 'BMW'}}}, 2)
        self.assertIn('/samara/avtomobili?', url)
        self.assertIn('p=2', url)

    def test_external_url_never_receives_cookies(self):
        client = AvitoClient.__new__(AvitoClient)
        client.session = Mock()
        with self.assertRaises(ValueError):
            client.get('https://example.com/avtomobili')
        client.session.get.assert_not_called()


from django.test import TestCase


class AvitoCollectorIntegrationTests(TestCase):
    def test_http_pages_share_database_and_do_not_stop_at_ten(self):
        from ads.models import SearchJob, Advertisement
        from parsers.collector import collect
        job = SearchJob.objects.create(fingerprint='avito-http', filters={'city': [AvitoAdapterTests().item()['location']['name']]})
        fixture = AvitoAdapterTests()
        page = fixture.page([fixture.item(123456789+i) for i in range(25)])
        driver = Mock()
        with patch('parsers.collector.load_page', side_effect=[page, fixture.page([])]), patch.dict('os.environ', {'AVITO_TRANSPORT': 'http'}):
            collect(driver, 'avito', job)
            collect(driver, 'avito', job)
        driver.find_elements.assert_not_called()
        self.assertEqual(Advertisement.objects.filter(platform='avito').count(), 25)
        job.refresh_from_db()
        self.assertEqual(job.source_state['avito']['requested_at'], job.requested_at.isoformat())
        self.assertEqual(job.source_state['avito']['count'], 25)
