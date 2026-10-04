from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from ads.catalog import load_children
from ads.generation_catalog import ensure_generation_catalog, _failed_until
from ads.generation_labels import ordinal, facelift, production_range
from ads.models import Advertisement, CatalogNode
from ads.search import matches, results
from parsers.collector import pending_details, save_ad
from parsers.generations import GenerationIndex


class LabelTests(SimpleTestCase):
    def test_roman_arabic_and_unicode_generation_numbers(self):
        for label, expected in [('VII (XV50)',7),('Ⅳ (G05)',4),('5-е поколение',5),('поколение 12',12)]:
            with self.subTest(label=label):
                self.assertEqual(ordinal(label),expected)
        for label in ('IIV','X5','2008','0 поколение','31 поколение'):
            self.assertIsNone(ordinal(label))

    def test_numbered_facelifts_and_pre_facelift(self):
        for label, expected in [('8 поколение, 2-й рестайлинг',2),('VII Рестайлинг 2',2),
                                ('III Facelift',1),('I дорестайлинг',0),('I pre-facelift',0)]:
            with self.subTest(label=label):
                self.assertEqual(facelift(label),expected)

    def test_explicit_production_period_and_invalid_range(self):
        self.assertEqual(production_range('VI (2006—2009)'),(2006,2009))
        self.assertEqual(production_range('II (2023 – н.в.)'),(2023,None))
        self.assertIsNone(production_range('2018–2010'))
        self.assertIsNone(production_range('2010'))


class UniversalGenerationTests(TestCase):
    def setUp(self):
        self.make = CatalogNode.objects.create(kind='make',key='toyota',slug='toyota',name='Toyota')
        self.model = CatalogNode.objects.create(kind='model',key='toyota/camry',slug='camry',name='Camry',parent=self.make,
            data={'url':'https://www.drom.ru/catalog/toyota/camry/'})
        self.base = self.gen('40','7 поколение',['XV40'],2006,2009)
        self.rest = self.gen('40-rest','7 поколение, рестайлинг',['XV40'],2009,2011)
        self.fifty = self.gen('50','8 поколение',['XV50'],2011,2014)
        self.fifty_rest = self.gen('50-rest','8 поколение, рестайлинг',['XV55','XV50'],2014,2017)
        self.fifty_rest2 = self.gen('50-rest2','8 поколение, 2-й рестайлинг',['XV55'],2017,2018)
        self.index = GenerationIndex.from_db()

    def gen(self,slug,description,codes,start,end):
        return CatalogNode.objects.create(kind='generation',key=f'toyota/camry/{slug}',slug=slug,
            name=f'{start}–{end}, {", ".join(codes)} · {description}',parent=self.model,
            data={'schema_version':2,'description':description,'codes':codes,'year_from':start,'year_to':end,
                  'generation_number':ordinal(description),'facelift_number':facelift(description),
                  'generation_urls':[f'https://www.drom.ru/catalog/toyota/camry/{slug}/']})

    def ad(self,label='',year='2007',source='autoru'):
        return {'brand':'Toyota','model':'Camry','generation':label,'year':year,'name':f'Toyota Camry, {year}',
                'platform':source,'external_id':'123456789','link':'https://example.com/123456789',
                'price':700000,'generation_codes':[],'image':'https://example.com/photo.jpg','city':'Москва'}

    def test_source_number_difference_uses_body_code(self):
        ad = self.ad('VI (XV40)')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_key'],self.base.key)
        self.assertEqual(ad['generation_evidence']['label'],'VI (XV40)')
        self.assertEqual(ad['generation_evidence']['method'],'body_code')

    def test_source_number_alias_is_learned_from_verified_body(self):
        verified = self.ad('VI (XV40)')
        self.index.normalize(verified)
        save_ad(verified)
        index = GenerationIndex.from_db()
        ad = self.ad('VI')
        index.normalize(ad)
        self.assertEqual(ad['generation_key'],self.base.key)
        self.assertEqual(ad['generation_evidence']['method'],'source_ordinal')

    def test_conflicting_source_ordinals_do_not_guess_a_family(self):
        self.index.learn_ordinal(self.base,'autoru','VI (XV40)')
        self.index.learn_ordinal(self.fifty,'autoru','VI (XV50)')
        ad = self.ad('VI')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_key'],'')
        self.assertEqual(ad['generation_evidence']['status'],'ambiguous')

    def test_source_alias_does_not_change_other_platform_numbering(self):
        self.index.learn_ordinal(self.base,'autoru','VI (XV40)')
        ad = self.ad('VII',source='drom')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_key'],self.base.key)
        self.assertEqual(ad['generation_evidence']['method'],'ordinal')

    def test_explicit_period_overrides_foreign_number(self):
        ad = self.ad('VI (2006–2009)')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_key'],self.base.key)
        self.assertEqual(ad['generation_evidence']['method'],'label_range')

    def test_second_facelift_reuses_family_code_without_matching_first(self):
        ad = self.ad('VII (XV50) Рестайлинг 2','2017')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_key'],self.fifty_rest2.key)
        self.assertEqual(ad['generation_codes'],['XV55'])
        self.assertTrue(matches(ad,{'vehicle':{'generation':{'key':self.fifty_rest2.key}}}))
        self.assertFalse(matches(ad,{'vehicle':{'generation':{'key':self.fifty_rest.key}}}))

    def test_overlap_without_facelift_evidence_is_ambiguous(self):
        ad = self.ad('XV40','2009')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_evidence']['status'],'ambiguous')
        self.assertEqual(ad['generation_key'],'')
        self.assertEqual(ad['generation_codes'],[])

    def test_unknown_body_does_not_fall_back_to_ordinal_or_year(self):
        for label in ('VII (ZZ99)','VI (ZZ99, 2006–2009)',''):
            ad = self.ad(label)
            self.index.normalize(ad)
            self.assertEqual(ad['generation_key'],'')
            self.assertEqual(ad['generation'],label)

    def test_exact_catalog_url_distinguishes_overlapping_facelift(self):
        ad = self.ad('7 поколение','2009','drom')
        self.index.normalize(ad,self.rest.data['generation_urls'][0])
        self.assertEqual(ad['generation_key'],self.rest.key)
        self.assertEqual(ad['generation_evidence']['method'],'catalog_url')

    def test_body_or_catalog_url_with_impossible_year_is_a_conflict(self):
        for url in (None,self.base.data['generation_urls'][0]):
            ad = self.ad('VI (XV40)','2024')
            self.index.normalize(ad,url)
            self.assertEqual(ad['generation_key'],'')
            self.assertEqual(ad['generation_evidence']['status'],'conflict')

    def test_changed_label_does_not_keep_a_stale_catalog_url(self):
        ad = self.ad('7 поколение')
        self.index.normalize(ad,self.base.data['generation_urls'][0])
        ad.update(generation='8 поколение (XV50)',year='2012')
        self.index.normalize(ad)
        self.assertEqual(ad['generation_key'],self.fifty.key)
        self.assertEqual(ad['generation_evidence']['url'],'')

    def test_normalization_is_idempotent_and_preserves_source_label(self):
        ad = self.ad('VI (XV40)')
        self.index.normalize(ad)
        before = dict(ad)
        self.index.normalize(ad)
        self.assertEqual(ad,before)

    def test_numeric_source_ids_are_repaired_from_title(self):
        ad = self.ad()
        ad.update(brand='23759895',model='23759904',name='Toyota Camry VI (XV40), 2007')
        self.index.normalize(ad)
        self.assertEqual((ad['brand'],ad['model']),('toyota','camry'))
        self.assertEqual(ad['generation_key'],self.base.key)

    def test_explicit_other_model_is_not_overridden_by_title(self):
        ad = self.ad('VI (XV40)')
        ad['model'] = 'Corolla'
        self.index.normalize(ad)
        self.assertEqual(ad['model'],'Corolla')
        self.assertEqual(ad['generation_key'],'')

    def test_codes_are_scoped_to_model_and_make(self):
        make = CatalogNode.objects.create(kind='make',key='other',slug='other',name='Other')
        model = CatalogNode.objects.create(kind='model',key='other/camry',slug='camry',name='Camry',parent=make)
        CatalogNode.objects.create(kind='generation',key='other/camry/40',slug='40',name='Other XV40',parent=model,
            data=self.base.data)
        index = GenerationIndex.from_db()
        ad = self.ad('VI (XV40)')
        index.normalize(ad)
        self.assertEqual(ad['generation_key'],self.base.key)

    def test_batch_normalization_has_no_per_ad_queries(self):
        with self.assertNumQueries(4):
            index = GenerationIndex.from_db()
            for i in range(30):
                index.normalize(self.ad('VI (XV40)'))

    def test_empty_listing_preserves_verified_generation_and_raw_evidence(self):
        ad = self.ad('VI (XV40)')
        self.index.normalize(ad)
        save_ad(ad)
        listing = self.ad()
        self.index.normalize(listing)
        save_ad(listing)
        row = Advertisement.objects.get()
        self.assertEqual(row.generation_key,self.base.key)
        self.assertEqual(row.generation_evidence['label'],'VI (XV40)')
        self.assertEqual(pending_details([listing],{'vehicle':{'generation':{'key':self.base.key}}},self.index),[])

    def test_changed_explicit_evidence_invalidates_old_key(self):
        ad = self.ad('VI (XV40)')
        self.index.normalize(ad)
        save_ad(ad)
        update = self.ad('VII (ZZ99)')
        self.index.normalize(update)
        save_ad(update)
        self.assertEqual(Advertisement.objects.get().generation_key,'')

    def test_backfill_dry_run_and_apply_preserve_prices_and_dates(self):
        row = Advertisement.objects.create(ad_id='autoru:1',platform='autoru',brand='Toyota',model='Camry',
            name='Toyota Camry VI (XV40), 2007',year='2007',price=700000,generation='VI (XV40)')
        old = timezone.now()-timedelta(days=7)
        Advertisement.objects.filter(pk=row.pk).update(created_at=old,last_seen=old,published_at=old)
        call_command('normalize_generations',stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.generation_key,'')
        call_command('normalize_generations',apply=True,stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.generation_key,self.base.key)
        self.assertEqual((row.price,row.created_at,row.last_seen,row.published_at),(700000,old,old,old))
        self.assertEqual([ad.pk for ad in results({'vehicle':{'generation':{'key':self.base.key}}})],[row.pk])

    def test_legacy_generation_slug_remains_compatible(self):
        ad = self.ad('VI (XV40)')
        self.index.normalize(ad)
        self.assertTrue(matches(ad,{'vehicle':{'generation':{'slug':'40'}}}))
        self.assertFalse(matches(ad,{'vehicle':{'generation':{'slug':'40-rest'}}}))


class CatalogueLoadingTests(TestCase):
    def setUp(self):
        _failed_until.clear()
        make = CatalogNode.objects.create(kind='make',key='geely',slug='geely',name='Geely')
        self.model = CatalogNode.objects.create(kind='model',key='geely/atlas',slug='atlas',name='Atlas',parent=make,
            refreshed_at=timezone.now(),data={'url':'https://www.drom.ru/catalog/geely/atlas/'})
        self.filters = {'vehicle':{'make':{'slug':'geely'},'model':{'slug':'atlas'}}}

    def test_refresh_enriches_legacy_nodes_despite_recent_timestamp(self):
        gen = CatalogNode.objects.create(kind='generation',key='geely/atlas/old',parent=self.model,name='Old')
        with patch('ads.generation_catalog.load_children',return_value=[gen]) as load:
            ensure_generation_catalog(self.filters)
            ensure_generation_catalog(self.filters)
        load.assert_called_once_with(self.model.pk,force=True)

    def test_fresh_enriched_catalog_avoids_network(self):
        CatalogNode.objects.create(kind='generation',key='geely/atlas/new',parent=self.model,name='New',data={'schema_version':2})
        with patch('ads.generation_catalog.load_children') as load:
            ensure_generation_catalog(self.filters)
        load.assert_not_called()

    def test_catalog_failure_has_bounded_retry_cooldown(self):
        with patch('ads.generation_catalog.load_children',side_effect=ValueError('unavailable')) as load:
            ensure_generation_catalog(self.filters)
            ensure_generation_catalog(self.filters)
        self.assertEqual(load.call_count,1)

    def test_official_alternative_model_links_share_one_generation_catalog(self):
        from users.models import UserFilters
        from ads.search import snapshot
        target_url = 'https://www.drom.ru/catalog/geely/sx11/'
        alias = {'twin':{'globalModelLink':{'url':target_url},'globalModelName':{'name':'Geely Coolray'}}}
        catalogue = {'generationsInOrder':[{'title':'2023–н.в.','generationInfo':'2 поколение',
            'items':[{'url':target_url+'g_2023/','title':'2023–н.в.','frames':'SX11'}]}]}
        with patch('ads.catalog.fetch_catalog',side_effect=[alias,catalogue]):
            loaded = load_children(self.model.pk,force=True)
        self.assertEqual(len(loaded),1)
        self.assertEqual(loaded[0].parent.key,'geely/sx11')
        self.assertFalse(CatalogNode.objects.filter(kind='generation',parent=self.model).exists())
        self.model.refresh_from_db()
        filters = UserFilters.objects.create(user_id=999,vehicle=self.model)
        self.assertEqual(snapshot(filters)['vehicle']['model']['slug'],'sx11')
        index = GenerationIndex.from_db()
        ad = {'brand':'Geely','model':'atlas','name':'Geely Atlas, 2024','platform':'drom',
              'generation':'2 поколение','year':'2024'}
        index.normalize(ad)
        self.assertEqual(ad['model'],'sx11')
        self.assertEqual(ad['generation_key'],loaded[0].key)

    def test_alias_cycle_does_not_recurse_or_fetch(self):
        self.model.data['canonical_model_key'] = self.model.key
        self.model.save(update_fields=['data'])
        with patch('ads.catalog.fetch_catalog') as fetch:
            with self.assertRaises(ValueError):
                load_children(self.model.pk)
        fetch.assert_not_called()

    def test_updated_title_retains_generation_identity_and_selected_vehicle(self):
        from users.models import UserFilters
        url = 'https://www.drom.ru/catalog/geely/atlas/g_2023/'
        gen = CatalogNode.objects.create(kind='generation',key='geely/atlas/stable',slug='stable',parent=self.model,
            name='Old title',data={'generation_urls':[url]})
        filters = UserFilters.objects.create(user_id=999,vehicle=gen)
        payload = {'generationsInOrder':[{'title':'2023–2026','generationInfo':'2 поколение',
                    'items':[{'url':url,'title':'2023–2026','frames':''}]}]}
        with patch('ads.catalog.fetch_catalog',return_value=payload):
            loaded = load_children(self.model.pk,force=True)
        self.assertEqual([node.pk for node in loaded],[gen.pk])
        filters.refresh_from_db()
        gen.refresh_from_db()
        self.assertEqual(filters.vehicle_id,gen.pk)
        self.assertEqual(gen.key,'geely/atlas/stable')
        self.assertIn('2026',gen.name)

    def test_parse_code_less_generation_frames_periods_and_second_facelift(self):
        payload = {'generationsInOrder':[{'title':'2023–н.в., FX11','generationInfo':'2 поколение, 2-й рестайлинг',
            'items':[{'url':'https://www.drom.ru/catalog/geely/atlas/g_2023/', 'title':'08.2023 – н.в.',
                      'frames':'FX11, FX11A','outlet':{'name':'Россия'}}]}]}
        with patch('ads.catalog.fetch_catalog',return_value=payload):
            nodes = load_children(self.model.pk,force=True)
        self.assertEqual(len(nodes),1)
        data = nodes[0].data
        self.assertEqual((data['generation_number'],data['facelift_number']),(2,2))
        self.assertEqual(data['frame_codes'],['FX11','FX11A'])
        self.assertEqual(data['periods'][0]['year_from'],2023)
        self.assertIsNone(data['periods'][0]['year_to'])
        self.assertEqual(data['periods'][0]['market'],'Россия')
        self.assertEqual(data['schema_version'],2)