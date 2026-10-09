from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from django.test import SimpleTestCase, TestCase
from ads.filter_values import bounds, describe, parse_price_amount, range_label
from ads.catalog_search import ranked_rows
from ads.search import matches, results
from ads.models import Advertisement, SearchJob
from parsers.sources import source_url, extract_listings


class FilterTests(SimpleTestCase):
    def test_price_thousands_separators(self):
        for text, expected in [('500000',500000),('500 000',500000),('400.000',400000),
                               ('1.500.000',1500000),('1 500 000',1500000),
                               ('500\u00a0000',500000),('500\u202f000 ₽',500000),
                               ('400,000 руб.',400000),('0',0)]:
            with self.subTest(text=text):
                self.assertEqual(parse_price_amount(text),expected)
        self.assertEqual(range_label(f'{parse_price_amount("400.000")}-'), 'от 400 000')

    def test_ambiguous_or_invalid_price_is_rejected(self):
        for text in ('1.5','400.00','50 00','-500','1e6','1.500,000','50000000000','₽'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_price_amount(text)

    def test_open_and_legacy_price_bounds(self):
        self.assertEqual(bounds('500 000-'),(500000,None))
        self.assertEqual(bounds('-1000000'),(None,1000000))
        self.assertEqual(bounds('500000'),(None,500000))
        self.assertTrue(matches({'price':750000},{'price':'500000-'}))
        self.assertFalse(matches({'price':750000},{'price':'-500000'}))
        for value in ('100-50','abc','-20-30'):
            with self.assertRaises(ValueError): bounds(value)

    def test_complete_description(self):
        value=describe({'price':'-500000','city':['Самара'],'vehicle':{
            'make':{'name':'BMW'},'model':{'name':'3-Series'},
            'generation':{'name':'long','description':'5 поколение','codes':['E90','E91']}}})
        for part in ('BMW 3-Series 5 поколение (E90, E91)','до 500 000','Самара'):
            self.assertIn(part,value)

    def test_models_popular_first_generations_chronological(self):
        def row(name,slug,kind='model',year=0):
            return SimpleNamespace(name=name,slug=slug,kind=kind,pk=slug,data={'year_from':year})
        rows=[row('1-Series','1-series'),row('3-Series','3-series'),row('5-Series','5-series'),row('Z8','z8')]
        self.assertEqual([r.name for r in ranked_rows(rows,make_slug='bmw')],['3-Series','5-Series','1-Series','Z8'])
        rows=[row('G20','g20','generation',2018),row('E90','e90','generation',2005),row('F30','f30','generation',2011)]
        self.assertEqual([r.name for r in ranked_rows(rows)],['E90','F30','G20'])

    def test_upstream_city_price_and_model_alias(self):
        filters={'city':['Самара'],'price':'-1000000','vehicle':{'make':{'name':'BMW','slug':'bmw'},'model':{'name':'3-Series','slug':'3-series'}}}
        self.assertIn('samara.drom.ru/bmw/3-series/',source_url('drom',filters))
        self.assertIn('maxprice=1000000',source_url('drom',filters))
        self.assertIn('/samara/cars/bmw/3er/',source_url('autoru',filters))
        self.assertIn('/samara/avtomobili?',source_url('avito',filters))
        html='<div class="ListingItem"><a class="ListingItemTitle__link" href="https://auto.ru/cars/used/sale/bmw/3er/123456789-ab/">BMW 3</a><div class="ListingItemPrice__content">500000</div></div>'
        self.assertEqual(extract_listings(html,'autoru')[0]['model'],'3-series')

    def test_autoru_universal_price_and_generation_from_listing(self):
        html='<div data-seo="listing-item"><a class="ListingItemTitle__link" href="https://auto.ru/cars/used/sale/bmw/3er/123456789-ab/">BMW 3 серии V (E90/E91) Рестайлинг, 2010</a><div class="ListingItemUniversalPrice__title-abc">900 000 ₽</div></div>'
        ad=extract_listings(html,'autoru')[0]
        self.assertEqual(ad['price'],900000)
        self.assertEqual(ad['generation_codes'],['E90','E91'])
        self.assertIn('Рестайлинг',ad['generation'])

class CollectionTests(TestCase):
    def test_details_are_resumable_and_finish_after_listing(self):
        from parsers.collector import collect
        job=SearchJob.objects.create(fingerprint='details',filters={'engine':['бензин']})
        card='<div data-ftid="bulls-list_bull"><a data-ftid="bull_title" href="https://auto.drom.ru/bmw/x5/12345678.html">BMW X5, 2020</a><span data-ftid="bull_price">5000000</span></div>'
        detail='<table><tr data-ftid="specification-engine"><th>Двигатель</th><td>бензин</td></tr></table>'
        with patch('parsers.collector.load_page',side_effect=[card,detail]):
            collect(None,'drom',job)
            job.refresh_from_db()
            self.assertEqual(len(job.source_state['drom']['detail_queue']),1)
            self.assertIsNone(job.source_state['drom']['requested_at'])
            collect(None,'drom',job)
        job.refresh_from_db()
        self.assertEqual(job.source_state['drom']['status'],'готово')
        self.assertEqual(len(results(job.filters)),1)

    def test_newly_loaded_order_ignores_last_seen_refresh(self):
        first=Advertisement.objects.create(ad_id='a',name='first',price=1)
        second=Advertisement.objects.create(ad_id='b',name='second',price=1)
        from django.utils import timezone
        Advertisement.objects.filter(pk=first.pk).update(last_seen=timezone.now())
        self.assertEqual([a.pk for a in results({})],[second.pk,first.pk])

    def test_pagination_continues_beyond_three_pages_and_stops_at_end(self):
        from parsers.collector import collect
        job=SearchJob.objects.create(fingerprint='pages',filters={})
        pages=[]
        for i in range(1,6):
            html=f'<div data-ftid="bulls-list_bull"><a data-ftid="bull_title" href="https://auto.drom.ru/bmw/x5/{12345670+i}.html">BMW X5, 2020</a><span data-ftid="bull_price">5000000</span></div>'
            if i<5: html+=f'<a href="/bmw/x5/page{i+1}/">next</a>'
            pages.append(html)
        with patch('parsers.collector.load_page',side_effect=pages):
            for _ in pages: collect(None,'drom',job)
        job.refresh_from_db()
        self.assertEqual(job.source_state['drom']['count'],5)
        self.assertEqual(job.source_state['drom']['status'],'готово')
        self.assertEqual(Advertisement.objects.count(),5)

class SourceReadinessTests(SimpleTestCase):
    def test_pending_generation_waits_for_first_verified_source_match(self):
        from ads.search_status import ready_for_batch
        pending = {'cycle':'token','pages':1,'status':'проверка карточек','detail_queue':[{}]}
        self.assertFalse(ready_for_batch(pending,'token',False))
        self.assertTrue(ready_for_batch(pending,'token',True))
        self.assertTrue(ready_for_batch({**pending,'status':'доступ ограничен'},'token',False))
        self.assertFalse(ready_for_batch({**pending,'status':'доступ ограничен'},'other-token',False))
