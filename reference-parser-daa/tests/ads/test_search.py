from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from django.db import IntegrityError, transaction
from django.utils import timezone
from ads.models import CatalogNode, Advertisement, Delivery
from ads.catalog import load_children
from ads.search import matches, request_search
from users.models import UserFilters
from parsers.sources import extract_listings, enrich, source_url, SourceBlocked


class SearchMatchingTests(SimpleTestCase):
    def setUp(self):
        self.ad = {'brand':'bmw','model':'x5','year':'2020','price':5000000,'generation_codes':['G05'],'generation':'4 поколение'}
        self.filters = {'vehicle':{'make':{'name':'BMW','slug':'bmw'}, 'model':{'name':'X5','slug':'x5'}}}

    def test_model_is_exact_not_prefix(self):
        self.assertTrue(matches(self.ad,self.filters))
        self.assertFalse(matches({**self.ad,'model':'X5 M'},self.filters))
        self.assertFalse(matches({**self.ad,'model':'X6'},self.filters))

    def test_generation_requires_evidence_not_only_year(self):
        self.filters['vehicle']['generation']={'codes':['G05'],'year_from':2018,'year_to':2023}
        self.assertTrue(matches(self.ad,self.filters))
        self.assertFalse(matches({**self.ad,'generation_codes':[]},self.filters))
        self.assertFalse(matches({**self.ad,'generation_codes':['F15']},self.filters))

    def test_unknown_price_does_not_match_budget(self):
        self.filters['price']='1000000-6000000'
        self.assertFalse(matches({**self.ad,'price':None},self.filters))

    def test_page_urls_advance_and_avito_has_search(self):
        self.assertIn('/bmw/x5/page2/',source_url('drom',self.filters,2))
        self.assertIn('page=2',source_url('autoru',self.filters,2))
        self.assertIn('q=BMW+X5',source_url('avito',self.filters,2))


class SourceMarkupTests(SimpleTestCase):
    def test_autoru_suffixed_specification_class(self):
        ad = {'platform':'autoru', 'name':'BMW X5', 'price':5000000,
              'year':'2020', 'city':'', 'generation':''}
        enrich(ad, '<li class="CardInfoRow__generation"><div>Поколение</div>'
                   '<div>IV (G05)</div></li>')
        self.assertEqual(ad['generation'], 'IV (G05)')

    def test_ip_block_is_not_empty_search(self):
        with self.assertRaises(SourceBlocked):
            extract_listings('<title>Доступ ограничен: проблема с IP</title>','avito')

    def test_unknown_markup_is_reported(self):
        with self.assertRaises(RuntimeError):
            extract_listings('<html><body>Unknown layout</body></html>','drom')

    def test_drom_current_price_marker_and_no_ten_limit(self):
        card = '''<div data-ftid="bulls-list_bull"><a data-ftid="bull_title" href="https://auto.drom.ru/moscow/bmw/x5/{id}.html">BMW X5, 2020</a><span data-ftid="bull_price">5&nbsp;250&nbsp;000</span><span data-ftid="bull_location">Москва</span></div>'''
        ads = extract_listings(''.join(card.format(id=10000000+i) for i in range(20)),'drom')
        self.assertEqual(len(ads),20)
        self.assertEqual(ads[0]['price'],5250000)
        self.assertEqual(ads[0]['model'],'x5')

    def test_avito_markers(self):
        html='''<div data-marker="item"><a data-marker="item-title" href="/moskva/avtomobili/bmw_x5_123456789">BMW X5, 2020</a><p data-marker="item-price">5 000 000 ₽</p></div>'''
        self.assertEqual(extract_listings(html,'avito')[0]['external_id'],'123456789')

    def test_autoru_ids(self):
        html='''<div class="ListingItem"><a class="ListingItemTitle__link" href="https://auto.ru/cars/used/sale/bmw/x5/123456789-ab12/">BMW X5</a><div class="ListingItem__year">2020</div><div class="ListingItemPrice__content">5 000 000 ₽</div></div>'''
        ad=extract_listings(html,'autoru')[0]
        self.assertEqual(ad['external_id'],'123456789')
        self.assertEqual(ad['brand'],'bmw')

    def test_spare_part_banner_does_not_determine_generation(self):
        node=CatalogNode(name='G05',data={'codes':['G05'],'generation_urls':['https://www.drom.ru/catalog/bmw/x5/g_2018_8395/']})
        ad={'platform':'drom','name':'BMW X5, 2020','year':'2020','price':5000000,'city':'Москва','generation':'','generation_codes':[]}
        enrich(ad,'<p>Фара BMW X5 G05</p>',[node])
        self.assertEqual(ad['generation_codes'],[])
        enrich(ad,'<table><tr data-ftid="specification-generation"><th>Поколение</th><td><a href="https://www.drom.ru/catalog/bmw/x5/g_2018_8395/">4 поколение</a></td></tr></table>',[node])
        self.assertEqual(ad['generation_codes'],['G05'])


class PersistenceTests(TestCase):
    def test_partial_listing_saved_before_next_page_failure(self):
        from parsers.collector import collect
        from ads.models import SearchJob
        job = SearchJob.objects.create(fingerprint='partial-test', filters={})
        card = '<div data-ftid="bulls-list_bull"><a data-ftid="bull_title" href="https://auto.drom.ru/bmw/x5/12345678.html">BMW X5, 2020</a><span data-ftid="bull_price">5000000</span></div><a href="/bmw/x5/page2/">2</a>'
        with patch('parsers.collector.PAGES',2), patch('parsers.collector.time.sleep'), patch('parsers.collector.load_page',side_effect=[card,SourceBlocked('blocked')]):
            collect(None,'drom',job)
            with self.assertRaises(SourceBlocked):
                collect(None,'drom',job)
        self.assertTrue(Advertisement.objects.filter(ad_id='drom:12345678').exists())
        job.refresh_from_db()
        self.assertEqual(job.source_state['drom']['count'],1)

    def test_catalog_new_parent_still_loads_children(self):
        make=CatalogNode.objects.create(key='bmw',kind='make',slug='bmw',name='BMW',refreshed_at=timezone.now(),data={'url':'https://www.drom.ru/catalog/bmw/'})
        with patch('ads.catalog.fetch_catalog',return_value={'models':[{'name':'X5','url':'https://www.drom.ru/catalog/bmw/x5/'}]}) as fetch:
            self.assertEqual(len(load_children(make.pk)),1)
            self.assertEqual(len(load_children(make.pk)),1)
            self.assertEqual(fetch.call_count,1)

    def test_identical_searches_share_job_and_filter_change_separates(self):
        first=UserFilters.objects.create(user_id=1,brand=['bmw'])
        second=UserFilters.objects.create(user_id=2,brand=['bmw'])
        self.assertEqual(request_search(first).pk,request_search(second).pk)
        second.price='100-200'
        self.assertNotEqual(request_search(first).pk,request_search(second).pk)

    def test_delivery_is_per_user_and_price(self):
        ad=Advertisement.objects.create(ad_id='drom:123',platform='drom',name='BMW',price=100)
        Delivery.objects.create(user_id=1,advertisement=ad,price=100)
        Delivery.objects.create(user_id=2,advertisement=ad,price=100)
        Delivery.objects.create(user_id=1,advertisement=ad,price=90)
        with self.assertRaises(IntegrityError),transaction.atomic():
            Delivery.objects.create(user_id=1,advertisement=ad,price=100)

    def test_catalog_endpoint_handles_invalid_parent(self):
        self.assertEqual(self.client.get('/catalog/?parent=bad').status_code,503)
