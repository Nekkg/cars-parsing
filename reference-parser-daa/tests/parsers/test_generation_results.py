from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from ads.models import Advertisement, CatalogNode, SearchJob
from ads.search import matches, results, result_order, result_page
from ads.search_status import current_status_text
from parsers.generations import normalize_generation
from parsers.sources import extract_listings, SourceBlocked
from parsers.collector import collect, pending_details, save_ad
from parsers.date_values import source_datetime


def vehicle_catalog():
    make = CatalogNode.objects.create(kind='make', key='volvo', slug='volvo', name='Volvo')
    model = CatalogNode.objects.create(kind='model', key='volvo/s60', slug='s60', name='S60', parent=make)
    base = CatalogNode.objects.create(kind='generation', key='s60/i', name='S60 I P24', parent=model,
        data={'description':'1 поколение', 'codes':['P24'], 'year_from':2000, 'year_to':2004})
    CatalogNode.objects.create(kind='generation', key='s60/i-rest', name='S60 I P24 рестайлинг', parent=model,
        data={'description':'1 поколение, рестайлинг', 'codes':['P24'], 'year_from':2004, 'year_to':2011})
    CatalogNode.objects.create(kind='generation', key='s60/ii', name='S60 II Y20', parent=model,
        data={'description':'2 поколение', 'codes':['Y20'], 'year_from':2010, 'year_to':2013})
    filters = {'vehicle':{'make':{'name':'Volvo','slug':'volvo'}, 'model':{'name':'S60','slug':'s60'},
                         'generation':{'name':base.name, **base.data}}}
    return list(CatalogNode.objects.filter(kind='generation').select_related('parent__parent')), filters


def card(source='avito', identifier='123456789', year='2003', generation=''):
    return {'platform':source,'external_id':identifier,'link':f'https://example.com/{identifier}',
            'name':f'Volvo S60, {year}','brand':'Volvo','model':'S60','year':year,'price':500000,
            'generation':generation,'generation_codes':[],'image':'https://example.com/car.jpg','city':'Москва'}


class GenerationNormalizationTests(TestCase):
    def setUp(self):
        self.generations, self.filters = vehicle_catalog()

    def test_autoru_ordinal_without_body_code_maps_to_p24(self):
        html = '<div class="ListingItem"><a class="ListingItemTitle__link" href="https://auto.ru/cars/used/sale/volvo/s60/123456789-ab/">Volvo S60 I, 2003</a><span class="ListingItemPrice__content">500000</span></div>'
        ad = extract_listings(html, 'autoru')[0]
        self.assertEqual(ad['generation'], 'I')
        normalize_generation(ad, self.generations)
        self.assertEqual(ad['generation_codes'], ['P24'])
        self.assertTrue(matches(ad, self.filters))

    def test_avito_year_range_is_an_explicit_generation_label(self):
        ad = card(generation='I (2000—2004)')
        normalize_generation(ad, self.generations)
        self.assertTrue(matches(ad, self.filters))

    def test_facelift_and_overlapping_second_generation_do_not_match_base(self):
        for label, year in [('I Рестайлинг', '2004'), ('II (2010—2013)', '2011')]:
            with self.subTest(label=label):
                ad = card(year=year, generation=label)
                normalize_generation(ad, self.generations)
                self.assertFalse(matches(ad, self.filters))
        base = card(year='2004', generation='I')
        normalize_generation(base, self.generations)
        self.assertTrue(matches(base, self.filters))

    def test_unknown_generation_and_other_models_are_not_inferred_from_year(self):
        ad = card()
        normalize_generation(ad, self.generations)
        self.assertEqual(ad['generation_codes'], [])
        ad = card(generation='I')
        ad['model'] = 'XC90'
        normalize_generation(ad, self.generations)
        self.assertEqual(ad['generation_codes'], [])

    def test_queue_filters_year_and_already_identified_generations_locally(self):
        known = card('autoru', generation='I')
        save_ad(known)
        ads = [known, card(identifier='123456790'), card(identifier='123456791', year='2012'),
               card('autoru', identifier='123456792', year='2004', generation='I Рестайлинг')]
        queue = pending_details(ads, self.filters, self.generations)
        self.assertEqual([a['external_id'] for a in queue], ['123456790'])
        self.assertEqual(len(results(self.filters)), 1)

    def test_blocked_cycle_keeps_page_and_collected_count_when_resumed(self):
        job = SearchJob.objects.create(fingerprint='resume', filters=self.filters)
        token = job.requested_at.isoformat()
        job.source_state = {'avito': {'cycle':token,'requested_at':token,'status':'доступ ограничен',
            'error_code':'blocked','count':50,'pages':1,'next_page':2,'route':0,
            'listing_finished':False,'detail_queue':[card()],'query_years':[2000,2004],'query_years_version':2}}
        job.save(update_fields=['source_state'])
        html = '<div data-marker="item-view/item-params"><li>Поколение: I (2000—2004)</li></div>'
        with patch('parsers.collector.load_page', return_value=html) as load:
            collect(None,'avito',job)
        job.refresh_from_db()
        state = job.source_state['avito']
        self.assertEqual(state['count'],50)
        self.assertEqual(state['next_page'],2)
        self.assertIsNone(state['requested_at'])
        self.assertEqual(state['detail_queue'],[])
        self.assertEqual(len(results(self.filters)),1)
        self.assertEqual(load.call_count,1)

    def test_completed_detail_is_removed_before_next_card_blocks(self):
        job = SearchJob.objects.create(fingerprint='per-card', filters={'engine':['бензин']})
        token = job.requested_at.isoformat()
        job.source_state = {'avito':{'cycle':token,'detail_queue':[card(),card(identifier='123456790')],
                                    'listing_finished':True,'count':2}}
        job.save(update_fields=['source_state'])
        html = '<div data-marker="item-view/item-params"><li>Тип двигателя: бензин</li></div>'
        with patch('parsers.collector.load_page',side_effect=[html,SourceBlocked('blocked')]):
            with self.assertRaises(SourceBlocked):
                collect(None,'avito',job)
        job.refresh_from_db()
        self.assertEqual([a['external_id'] for a in job.source_state['avito']['detail_queue']],['123456790'])
        self.assertEqual(len(results(job.filters)),1)

    def test_status_distinguishes_processed_and_matching_rows(self):
        ad = card('autoru',generation='I')
        normalize_generation(ad,self.generations)
        save_ad(ad)
        save_ad(card('autoru',identifier='123456790',year='2012'))
        job = SearchJob.objects.create(fingerprint='status',filters=self.filters,
            source_state={'autoru':{'count':77,'pages':4,'status':'готово'}})
        text = current_status_text(job)
        self.assertIn('Обработано записей: 77, страниц: 4.',text)
        self.assertIn('Auto.ru: готово\nПо вашим фильтрам в общей базе: 1.',text)


class SharedResultOrderingTests(TestCase):
    def test_first_three_sources_then_global_tail_without_duplicates(self):
        ads = [SimpleNamespace(pk=i,platform=s) for i,s in enumerate(
            ['drom','drom','avito','drom','autoru','avito','autoru'],1)]
        ordered, size = result_order(ads,['avito','autoru','drom'])
        self.assertEqual([a.pk for a in result_page(ordered,size,0)],[3,5,1])
        self.assertEqual([a.pk for a in result_page(ordered,size,1)],[2,4,6])
        self.assertEqual([a.pk for a in result_page(ordered,size,2)],[7])
        self.assertEqual(len({a.pk for a in ordered}),len(ads))

    def test_missing_source_shortens_first_batch_without_skipping_rows(self):
        ads = [SimpleNamespace(pk=i,platform=s) for i,s in enumerate(['drom','drom','autoru','drom','autoru'],1)]
        ordered, size = result_order(ads,['avito','autoru','drom'])
        self.assertEqual([a.pk for a in result_page(ordered,size,0)],[3,1])
        self.assertEqual([a.pk for a in result_page(ordered,size,1)],[2,4,5])

    def test_price_sort_remains_global(self):
        ads = [SimpleNamespace(pk=i,platform='drom') for i in range(5)]
        ordered, size = result_order(ads,['avito','autoru','drom'],'cheap')
        self.assertEqual(ordered,ads)
        self.assertEqual(size,3)

    def test_source_publication_overrides_later_import_and_refresh_does_not_bump(self):
        newer = Advertisement.objects.create(ad_id='avito:1', name='newer',price=1,
            published_at=timezone.now()-timedelta(days=1))
        older = Advertisement.objects.create(ad_id='avito:2', name='older',price=2,
            published_at=timezone.now()-timedelta(days=2))
        Advertisement.objects.filter(pk=older.pk).update(last_seen=timezone.now())
        self.assertEqual([a.pk for a in results({})],[newer.pk,older.pk])

    def test_timestamp_accepts_seconds_milliseconds_and_rejects_untrusted_values(self):
        now = timezone.now()
        self.assertEqual(source_datetime(int(now.timestamp())),source_datetime(int(now.timestamp())*1000))
        for value in ('yesterday', '2026-01-01', 10**20, 123, True):
            self.assertIsNone(source_datetime(value))

    def test_save_ad_preserves_publication_on_price_refresh(self):
        ad = card()
        published = timezone.now()-timedelta(days=3)
        ad['published_at'] = published.isoformat()
        save_ad(ad)
        save_ad({**ad,'published_at':None,'price':450000})
        row = Advertisement.objects.get()
        self.assertEqual(row.published_at,published)
        self.assertEqual(row.price,450000)
