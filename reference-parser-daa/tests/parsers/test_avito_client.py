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
        client.session.get.return_value.text = '<title>Access restricted</title>'
        client.session.get.return_value.iter_content.return_value = iter([b'<title>Access restricted</title>'])
        client.session.get.return_value.json.return_value = {}
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


class AvitoRedirectTests(SimpleTestCase):
    def make_client(self, responses):
        client = AvitoClient.__new__(AvitoClient)
        client.session = Mock()
        client.session.get.side_effect = responses
        client.last_request = 0
        client.page_source = ''
        client.load_cookies = Mock()
        return client

    def response(self, content, status=200, headers=None):
        response = Mock(status_code=status, text=content, headers=headers or {})
        response.iter_content.return_value = iter([content.encode()])
        return response

    def test_follows_ssr_canonical_redirect_without_a_browser(self):
        shell = '<script data-mfe-state="true">' + json.dumps({'loaderData': {'redirect': '/samara/avtomobili/bmw'}}) + '</script>'
        client = self.make_client([self.response(shell), self.response('<h1>Cars</h1>')])
        with patch('parsers.avito_client.time.sleep'):
            client.get('https://www.avito.ru/samara/avtomobili?q=BMW')
        self.assertEqual(client.session.get.call_args[0][0], 'https://www.avito.ru/samara/avtomobili/bmw')
        self.assertEqual(client.page_source, '<h1>Cars</h1>')

    def test_redirect_never_forwards_session_to_another_host(self):
        client = self.make_client([self.response('', 302, {'Location': 'https://example.com/'})])
        with self.assertRaises(SourceBlocked):
            client.get('https://www.avito.ru/')
        client.session.get.assert_called_once()

    def test_credentials_and_wrong_port_are_rejected_before_request(self):
        for url in ('https://user:pass@www.avito.ru/', 'https://www.avito.ru:444/'):
            client = self.make_client([])
            with self.assertRaises(ValueError):
                client.get(url)
            client.session.get.assert_not_called()


class RussianMakeTests(SimpleTestCase):
    def test_lada_badges_are_not_part_of_the_model(self):
        from parsers.avito_client import vehicle_name
        for title in ('ВАЗ (LADA) Granta 1.6 MT, 2017, 98 270 км',
                      'Lada (ВАЗ) Granta 1.6 AT, 2015, 90 000 км',
                      'ВАЗ(LADA) Vesta 1.6 AMT, 2018',
                      'ВАЗ (LADA) 2114 Samara 1.6 MT, 2012'):
            with self.subTest(title=title):
                brand,model=vehicle_name(title)
                self.assertEqual(brand,'lada')
                self.assertNotIn('LADA',model.upper())
                self.assertNotIn('ВАЗ',model.upper())
        self.assertEqual(vehicle_name('ГАЗ 3110 Волга 2.4 MT, 2000'),('ГАЗ','3110 Волга'))
        self.assertEqual(vehicle_name('УАЗ Patriot 2.7 MT, 2019'),('УАЗ','Patriot'))


class LadaNormalizationTests(TestCase):
    def setUp(self):
        from ads.models import CatalogNode
        from parsers.generations import GenerationIndex
        self.make=CatalogNode.objects.create(kind='make',key='lada',slug='lada',name='Лада')
        self.model=CatalogNode.objects.create(kind='model',key='lada/granta',slug='granta',name='Гранта',parent=self.make)
        self.index=GenerationIndex.from_db()

    def test_native_make_and_stored_badge_model_are_repaired(self):
        for brand,model in (('ВАЗ','(LADA) Granta'),('ВАЗ (LADA)','Granta'),('Lada (ВАЗ)','Granta'),('Лада','Гранта')):
            with self.subTest(brand=brand,model=model):
                ad={'brand':brand,'model':model,'name':'ВАЗ (LADA) Granta 1.6 MT, 2017','platform':'avito','year':'2017'}
                self.index.normalize(ad)
                self.assertEqual((ad['brand'],ad['model']),('lada','granta'))

    def test_autoru_title_with_parenthesized_make_keeps_generation(self):
        ad={'brand':'ВАЗ (LADA)','model':'Granta','name':'Lada (ВАЗ) Granta I Рестайлинг, 2018',
            'platform':'autoru','year':'2018'}
        self.index.normalize(ad)
        self.assertEqual(ad['generation_evidence']['label'],'I Рестайлинг')

    def test_different_model_is_not_repaired_to_granta(self):
        ad={'brand':'ВАЗ','model':'(LADA) Vesta','name':'ВАЗ (LADA) Granta 1.6 MT, 2017','platform':'avito'}
        self.index.normalize(ad)
        self.assertEqual(ad['model'],'(LADA) Vesta')

    def test_repaired_listing_is_a_candidate_for_generation_details_with_radius(self):
        from parsers.collector import pending_details
        ad={'brand':'ВАЗ','model':'(LADA) Granta','name':'ВАЗ (LADA) Granta 1.6 MT, 2017','platform':'avito',
            'year':'2017','external_id':'123456789','price':400000,'city':'Самара',
            'link':'https://www.avito.ru/samara/avtomobili/granta_123456789'}
        filters={'vehicle':{'make':{'slug':'lada','name':'Лада'},'model':{'slug':'granta','name':'Гранта'},
            'generations':[{'key':'lada/granta/test','year_from':2013,'year_to':2018}]},
            'generation_keys':['lada/granta/test'],'city':['Самара'],'radius_km':200,
            'location':{'name':'Самара','latitude':53.195,'longitude':50.107}}
        queue=pending_details([ad],filters,self.index)
        self.assertEqual(len(queue),1)
        self.assertEqual(queue[0]['model'],'granta')

class GrantaSourceGenerationTests(TestCase):
    def setUp(self):
        from ads.models import CatalogNode
        from parsers.generations import GenerationIndex
        make=CatalogNode.objects.create(kind='make',key='lada',slug='lada',name='Лада')
        model=CatalogNode.objects.create(kind='model',key='lada/granta',slug='granta',name='Гранта',parent=make)
        self.nodes=[]
        for slug,start,end,phase in (('base',2011,2014,0),('2191',2013,2018,1),('modern',2018,None,2)):
            self.nodes.append(CatalogNode.objects.create(kind='generation',key='lada/granta/'+slug,slug=slug,
                name='Гранта '+slug,parent=model,data={'year_from':start,'year_to':end,'generation_number':1,
                'description':'1 поколение'+(', '+str(phase)+'-й рестайлинг' if phase else ''),'codes':[]}))
        self.index=GenerationIndex.from_db()

    def ad(self,label,year,source='avito'):
        return {'brand':'lada','model':'granta','platform':source,'name':'Lada Granta, '+year,
            'generation':label,'year':year}

    def test_avito_explicit_first_generation_range_matches_catalogue_2191_phase(self):
        ad=self.ad('I (2011—2018)','2017')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_key'],self.nodes[1].key)
        self.assertEqual(ad['generation_evidence']['method'],'source_period')
        self.assertEqual(ad['generation_evidence']['label'],'I (2011—2018)')

    def test_autoru_first_generation_and_facelift_use_source_periods(self):
        for label,year,target in (('I','2016',1),('I Рестайлинг','2018',2),('I Рестайлинг','2021',2)):
            ad=self.ad(label,year,'autoru')
            self.index.normalize(ad)
            self.assertEqual(ad['generation_key'],self.nodes[target].key)

    def test_no_generation_year_alone_and_foreign_body_do_not_guess(self):
        for label in ('','I (ZZ99)','II (2011—2018)'):
            ad=self.ad(label,'2017')
            self.index.normalize(ad)
            self.assertEqual(ad['generation_key'],'')

    def test_boundary_overlap_is_kept_ambiguous(self):
        ad=self.ad('I (2011—2018)','2014')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_evidence']['status'],'ambiguous')
        self.assertEqual(set(ad['generation_evidence']['candidate_keys']),{node.key for node in self.nodes[:2]})

    def test_out_of_source_period_year_does_not_match(self):
        ad=self.ad('I (2011—2018)','2021')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_key'],'')
        self.assertEqual(ad['generation_evidence']['status'],'conflict')

    def test_domestic_make_generation_details_match_real_city_radius_filter(self):
        from parsers.sources import enrich
        from ads.search import matches
        ad=self.ad('','2017')
        ad.update(brand='ВАЗ',model='(LADA) Granta',name='ВАЗ (LADA) Granta 1.6 MT, 2017',city='Самара')
        html='<ul data-marker="item-view/item-params"><li>Поколение: I (2011—2018)</li><li>Год выпуска: 2017</li></ul>'
        enrich(ad,html,self.index)
        filters={'vehicle':{'make':{'slug':'lada','name':'Лада'},'model':{'slug':'granta','name':'Гранта'},
            'generations':[{'key':self.nodes[1].key,'year_from':2013,'year_to':2018}]},
            'generation_keys':[self.nodes[1].key],'city':['Самара'],'radius_km':200,
            'location':{'name':'Самара','latitude':53.195,'longitude':50.107}}
        self.assertTrue(matches(ad,filters))
    def test_open_source_period_cannot_include_a_different_generation_family(self):
        from ads.models import CatalogNode
        from parsers.generations import GenerationIndex
        CatalogNode.objects.filter(pk=self.nodes[2].pk).update(data={**self.nodes[2].data,'year_to':2025})
        model=self.nodes[2].parent
        CatalogNode.objects.create(kind='generation',key='lada/granta/future',slug='future',name='Гранта II',
            parent=model,data={'year_from':2026,'year_to':None,'generation_number':2,'description':'2 поколение','codes':[]})
        index=GenerationIndex.from_db()
        ad=self.ad('I Рестайлинг (2018–н.в.)','2027')
        index.normalize(ad)
        self.assertEqual(ad['generation_key'],'')
        self.assertEqual(ad['generation_evidence']['status'],'conflict')
class AvitoRequestSchedulingTests(SimpleTestCase):
    def scheduling_client(self):
        client = AvitoClient.__new__(AvitoClient)
        client.session = Mock()
        client.last_request = 100.0
        client.last_page_request = 100.0
        response = AvitoRedirectTests().response('<html></html>')
        client.session.get.return_value = response
        return client

    def test_verification_steps_do_not_wait_five_seconds(self):
        client = self.scheduling_client()
        with patch('parsers.avito_client.time.monotonic', return_value=100.0), patch('parsers.avito_client.time.sleep') as sleep:
            client.request('post', 'https://www.avito.ru/web/3/firewallPow/get', deadline=190)
        sleep.assert_called_once_with(0.25)
        self.assertEqual(client.last_page_request, 100.0)

    def test_provider_resources_keep_page_limiter_independent(self):
        client = self.scheduling_client()
        with patch('parsers.avito_client.time.monotonic', return_value=104.0), patch('parsers.avito_client.time.sleep') as sleep:
            client.request('get', 'https://static.geetest.com/captcha_v4/test.png', deadline=190, _challenge=True)
            client.request('get', 'https://www.avito.ru/all/avtomobili', deadline=190)
        sleep.assert_called_once_with(1.0)

    def test_regular_pages_still_have_five_second_spacing(self):
        client = self.scheduling_client()
        with patch('parsers.avito_client.time.monotonic', return_value=100.0), patch('parsers.avito_client.time.sleep') as sleep:
            client.request('get', 'https://www.avito.ru/all/avtomobili', deadline=190)
        sleep.assert_called_once_with(5.0)

    def test_successful_html_saves_session_but_rejected_response_does_not(self):
        client = AvitoRedirectTests().make_client([AvitoRedirectTests().response('<html></html>')])
        client.cookie_store = Mock()
        client.get('https://www.avito.ru/all/avtomobili')
        client.cookie_store.save.assert_called_once_with(client.session.cookies)
        client.cookie_store.reset_mock()
        client.request = Mock(return_value=Mock(status_code=403))
        client.recover_pow = Mock(return_value=False)
        with patch('parsers.avito_captcha.recover_captcha', return_value=False), self.assertRaises(SourceBlocked):
            client.get('https://www.avito.ru/all/avtomobili')
        client.cookie_store.save.assert_not_called()

class AvitoCanonicalRouteCacheTests(SimpleTestCase):
    def test_verified_route_avoids_repeated_text_redirect_and_keeps_new_years(self):
        from parsers.query_filters import avito_year_filter
        canonical = '/all/avtomobili/volvo/s60-ASgBAgICAkTgtg26mSjitg2eryg'
        shell = '<script data-mfe-state="true">' + json.dumps({'loaderData':{'redirect':canonical}}) + '</script>'
        fixture = AvitoRedirectTests()
        client = fixture.make_client([fixture.response(shell), fixture.response('<html></html>'), fixture.response('<html></html>')])
        client.catalogue_routes = {}
        with patch('parsers.avito_client.time.sleep'):
            client.get('https://www.avito.ru/all/avtomobili?q=Volvo+S60&p=1&f=' + avito_year_filter(2000, 2004))
            client.get('https://www.avito.ru/all/avtomobili?q=Volvo+S60&p=2&f=' + avito_year_filter(2005, 2010))
        from urllib.parse import parse_qs, urlparse
        url = client.session.get.call_args.args[0]
        self.assertEqual(urlparse(url).path, canonical)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query['p'], ['2'])
        self.assertEqual(query['f'], [avito_year_filter(2005,2010,facets='ASgBAgICAkTgtg26mSjitg2eryg')])
        self.assertEqual(client.session.get.call_count, 3)

class AvitoVerificationTransportTests(SimpleTestCase):
    def test_post_timeout_is_retryable_without_replaying_verification(self):
        from curl_cffi.requests.exceptions import Timeout
        from parsers.sources import SourceTemporaryError
        client = AvitoRequestSchedulingTests().scheduling_client()
        client.session.post.side_effect = Timeout('synthetic timeout')
        with patch('parsers.avito_client.time.sleep'), self.assertRaises(SourceTemporaryError):
            client.request('post', 'https://www.avito.ru/web/3/firewallPow/verify', deadline=999999999)
        client.session.post.assert_called_once()
