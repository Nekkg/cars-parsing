from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from django.test import TestCase,SimpleTestCase
from django.core.management import call_command
from django.utils import timezone
from ads.models import CatalogNode,Advertisement
from ads.search import matches
from parsers.generations import GenerationIndex,prefix
from parsers.sources import source_url,SourceBlocked
from parsers.vehicle_audit import verify,process_audit,query_for
from parsers.models import VehicleAudit


class ExactSourceNamesTests(TestCase):
    def model(self,make,slug,name):
        brand,_=CatalogNode.objects.get_or_create(kind='make',key=make,defaults={'slug':make,'name':make.replace('_',' ')})
        return CatalogNode.objects.create(kind='model',parent=brand,key=make+'/'+slug,slug=slug,name=name)

    def test_official_model_aliases_and_search_routes(self):
        cases=[('mazda','mazda3','Mazda3','3','3'),('mazda','mazda6','Mazda6','6','6'),
            ('audi','a4_allroad_quattro','A4 allroad quattro','a4_allroad','a4_allroad'),
            ('honda','stepwgn','Stepwgn','stepwagon','stepwagon'),
            ('land_rover','range_rover_evoque','Range Rover Evoque','evoque','evoque'),
            ('mercedes-benz','gle_coupe','GLE Coupe','gle-class_coupe','gle_klasse_coupe'),
            ('changan','qiyuan_q05','Qiyuan Nevo Q05','q05','q05')]
        rows=[self.model(*case[:3]) for case in cases]
        index=GenerationIndex.from_db()
        for row,case in zip(rows,cases):
            with self.subTest(case=case):
                ad={'brand':case[0],'model':case[3],'name':row.parent.name+' '+row.name,'platform':'autoru'}
                index.normalize(ad)
                self.assertEqual(ad['model'],row.slug)
                if case[0]!='changan':self.assertIn('/'+case[4]+'/all/',source_url('autoru',query_for(row)))

    def test_lexus_badges_preserve_body_and_hybrid_variants(self):
        from parsers.model_names import lexus_badge,lexus_model_slug
        for name,slug,family,badge in (
            ('IS 250C','is250c','is','250c'),('LS 600hL','ls600hl','ls','600hl'),
            ('RX 350L','rx350l','rx','350l'),('RZ 450e','rz450e','rz','450e'),
            ('NX 450h+','nx450hplus','nx','450hplus'),('TX 550h+','tx550hplus','tx','550hplus')):
            with self.subTest(name=name):
                self.assertEqual(lexus_badge(name),(family,badge))
                self.assertEqual(lexus_model_slug({'brand':'Lexus','name':'Lexus '+name+', 2024'}),slug)
        self.assertEqual(lexus_model_slug({'brand':'Lexus','name':'Lexus RX 3500, 2024'}),'')

    def test_lexus_family_uses_structured_modification_without_engine_guess(self):
        from parsers.sources import enrich
        from parsers.model_names import lexus_model_slug,lexus_candidate_score
        rows=[self.model('lexus',slug,slug.upper()) for slug in ('nx200','nx200t','lx570','rx300','rx350','rx350l','rx450h')]
        index=GenerationIndex.from_db()
        cases=[('NX','NX 200 2.0 CVT (150 л.с.)','nx200'),
            ('NX','NX 200 2.0t 4WD AT (238 л.с.)','nx200t'),
            ('LX','LX 570 5.7 4WD AT (367 л.с.)','lx570'),
            ('RX','RX 300 2.0 4WD AT (238 л.с.) 5 мест','rx300'),
            ('RX','RX 350 3.5 4WD AT (277 л.с.)','rx350'),
            ('RX','RX 350L 3.5 4WD AT (294 л.с.)','rx350l'),
            ('RX','RX 450h 3.5 4WD CVT (299 л.с.)','rx450h')]
        for family,modification,expected in cases:
            html='<div data-marker="item-view/item-params"><ul><li>Марка: Lexus</li><li>Модель: '+family+'</li><li>Модификация: '+modification+'</li></ul></div>'
            ad=enrich({'platform':'avito','name':'Lexus '+family,'brand':'Lexus','model':family,'image':'','images':[]},html,index)
            self.assertEqual(ad['model'],expected)
            self.assertEqual(ad['generation_evidence']['model_key'],'lexus/'+expected)
        self.assertEqual(lexus_model_slug({'brand':'Lexus','model':'RX','name':'Lexus RX 3.5 AT, 2020','engine':'Бензин'}),'')
        self.assertGreater(lexus_candidate_score({'name':'Lexus NX 2.0 CVT, 2017'},'nx200'),
            lexus_candidate_score({'name':'Lexus NX 2.0 AT, 2017'},'nx200'))
        self.assertGreater(lexus_candidate_score({'name':'Lexus LX 5.7 AT, 2020'},'lx570'),
            lexus_candidate_score({'name':'Lexus LX 4.5 AT, 2020'},'lx570'))

    def test_current_empty_autoru_page_excludes_recommendations(self):
        from parsers.sources import extract_listings
        html='<h1>BMW 503</h1><div class="ListingHead">Купить BMW 503</div><div class="ListingCarsFiltersDumb-abc"><button disabled>Нет предложений</button></div><div class="ListingCarouselNew">BMW X5 3 000 000</div>'
        self.assertEqual(extract_listings(html,'autoru'),[])
        with self.assertRaises(RuntimeError):extract_listings('<h1>Страница не найдена</h1>','autoru')

    def test_hybrid_infiniti_badge_and_emgrand_collisions(self):
        from parsers.model_names import infiniti_badge
        self.assertEqual(infiniti_badge('M35h'),('m','35',True))
        old=self.model('geely','emgrand','Emgrand EC7')
        new=self.model('geely','emgrand_iv','Emgrand')
        index=GenerationIndex.from_db()
        for title,expected in (('Geely Emgrand EC7, 2015',old),('Geely Emgrand, 2024',new)):
            ad={'brand':'geely','model':'emgrand','platform':'avito','name':title}
            self.assertEqual(index.identify_model(ad),expected)

    def test_avito_uses_marketplace_model_names(self):
        from urllib.parse import parse_qs,urlparse
        for make,slug,name,expected in (('mazda','mazda3','Mazda3','mazda 3'),
            ('changan','qiyuan_q05','Qiyuan Nevo Q05','changan Q05')):
            row=self.model(make,slug,name)
            url=source_url('avito',query_for(row))
            self.assertEqual(parse_qs(urlparse(url).query)['q'],[expected])

    def test_source_families_require_explicit_badge_or_body(self):
        rows=[self.model('infiniti','fx35','FX35'),self.model('infiniti','fx30d','FX30d'),
            self.model('bmw','3-series','3-Series'),self.model('bmw','3-series_gran_turismo','3-Series Gran Turismo')]
        index=GenerationIndex.from_db()
        cases=[('infiniti','fx','Infiniti FX35 I (S50), 2004','fx35'),
            ('infiniti','fx','Infiniti FX30d II (S51), 2012','fx30d'),
            ('bmw','3er','BMW 3 серии Gran Turismo 320i VI (F3x), 2017','3-series_gran_turismo')]
        for brand,raw,title,wanted in cases:
            ad={'platform':'autoru','brand':brand,'model':raw,'name':title}
            index.normalize(ad);self.assertEqual(ad['model'],wanted)
        ad={'platform':'autoru','brand':'infiniti','model':'fx','name':'Infiniti FX, 2012','engine':'3.5 л, бензин'}
        self.assertIsNone(index.identify_model(ad))
        self.assertIn('/fx/all/',source_url('autoru',query_for(rows[0])))
        self.assertIn('/3er/all/',source_url('autoru',query_for(rows[3])))

    def test_models_with_plus_are_distinct_in_title_matching_and_filters(self):
        base=self.model('honda','freed','Freed')
        plus=self.model('honda','freed_plus','Freed+')
        index=GenerationIndex.from_db()
        for name,slug in (('Freed','freed'),('Freed+','freed_plus')):
            ad={'brand':'Honda','model':name,'name':'Honda '+name+' 1.5 AT, 2020','platform':'avito'}
            index.normalize(ad)
            self.assertEqual(ad['model'],slug)
            self.assertEqual(matches(ad,query_for(plus)),slug=='freed_plus')
        self.assertIsNone(prefix('Freed 1.5 AT','Freed+'))

    def test_numeric_model_names_with_hyphens_are_not_collapsed(self):
        old=self.model('saab','93','93');modern=self.model('saab','9-3','9-3')
        index=GenerationIndex.from_db()
        for row in (old,modern):
            ad={'brand':'Saab','model':row.slug,'name':'Saab '+row.name+', 2020','platform':'drom'}
            index.normalize(ad)
            self.assertEqual(ad['model'],row.slug)
            self.assertTrue(matches(ad,query_for(row)))
            self.assertFalse(matches(ad,query_for(modern if row==old else old)))
            ad.pop('generation_evidence')
            self.assertFalse(matches(ad,query_for(modern if row==old else old)))

    def test_compound_makes_are_not_split(self):
        from parsers.avito_client import vehicle_name
        for make,model in (('Renault Samsung','SM3'),('IM Motors','LS6'),('Polar Stone','01'),('TENET Plus','L4')):
            self.assertEqual(vehicle_name(make+' '+model+', 2024'),(make,model))

    def test_long_make_repair_does_not_override_a_different_model(self):
        row=self.model('lynk_and_co','03','03')
        index=GenerationIndex.from_db()
        ad={'brand':'Lynk','model':'& Co 03 1.5 AMT','name':'Lynk & Co 03 1.5 AMT, 2020','platform':'avito'}
        index.normalize(ad)
        self.assertEqual((ad['brand'],ad['model']),('lynk_and_co','03'))
        ad.update(brand='Lynk',model='& Co 05',name='Lynk & Co 03 1.5 AMT, 2020')
        index.normalize(ad)
        self.assertEqual(ad['model'],'& Co 05')


class AuditEvidenceTests(TestCase):
    def setUp(self):
        self.enterContext(patch('parsers.vehicle_audit.export_if_due'))
        from tempfile import TemporaryDirectory
        from pathlib import Path
        directory=TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.enterContext(patch('parsers.audit_scope.RUNTIME_DIR',Path(directory.name)))
        make=CatalogNode.objects.create(kind='make',key='bmw',slug='bmw',name='BMW')
        self.model=CatalogNode.objects.create(kind='model',key='bmw/x5',slug='x5',name='X5',parent=make)
        self.ad={'platform':'drom','external_id':'12345678','name':'BMW X5, 2020','brand':'bmw','model':'x5',
            'price':3000000,'year':'2020','link':'https://auto.drom.ru/bmw/x5/12345678.html',
            'image':'https://s1.auto.drom.ru/car.jpg'}

    def test_success_requires_real_card_photo_and_saved_filter_match(self):
        with patch('parsers.vehicle_audit.listing',return_value=[self.ad]),patch('parsers.vehicle_audit.card',return_value=self.ad),patch('bot.photo_delivery.download_photo',return_value=b'jpeg') as photo:
            status,proof=verify(None,self.model,'drom')
        self.assertEqual(status,'confirmed')
        self.assertTrue(proof['listing_checked'] and proof['card_checked'] and proof['photo_checked'] and proof['filter_checked'])
        self.assertEqual(proof['ad_url'],self.ad['link']);photo.assert_called_once()
        self.assertEqual(Advertisement.objects.count(),1)

    def test_current_autoru_summary_rows_are_read(self):
        from parsers.sources import enrich
        html='''<h1 class="CardHead__title">BMW X5, 2023</h1>
          <li class="CardInfoSummarySimpleRow-123"><div class="CardInfoSummarySimpleRow__label-x">Пробег</div><div class="CardInfoSummarySimpleRow__content-x">33 865 км</div></li>
          <li class="CardInfoSummarySimpleRow-123"><div>Владельцы</div><div>1 владелец</div></li>
          <li class="CardInfoSummaryComplexRow-123"><div><div class="CardInfoSummaryComplexRow__cellTitle-x">Двигатель</div><div class="CardInfoSummaryComplexRow__cellValue-x">3.0 л, 394 л.с., гибрид</div></div></li>
          <li class="CardInfoSummaryComplexRow-123"><div><div class="CardInfoSummaryComplexRow__cellTitle-x">Коробка</div><div class="CardInfoSummaryComplexRow__cellValue-x">автоматическая</div></div></li>'''
        ad=enrich({**self.ad,'platform':'autoru','city':''},html,GenerationIndex.from_db())
        self.assertEqual(ad['mileage'],33865);self.assertEqual(ad['owners'],1)
        self.assertIn('гибрид',ad['engine']);self.assertEqual(ad['gearbox'],'автоматическая')

    def test_wrong_card_canonical_url_is_rejected(self):
        from parsers.vehicle_audit import card
        html='<h1>BMW X5</h1><div data-ftid="specification-car"></div><link rel="canonical" href="https://auto.drom.ru/bmw/x5/87654321.html">'
        with patch('parsers.collector.load_page',return_value=html):
            with self.assertRaises(ValueError):card(None,self.ad)

    def test_failed_photo_is_not_a_confirmed_or_empty_result(self):
        with patch('parsers.vehicle_audit.listing',return_value=[self.ad]),patch('parsers.vehicle_audit.card',return_value=self.ad),patch('bot.photo_delivery.download_photo',side_effect=OSError):
            status,proof=verify(None,self.model,'drom')
        self.assertEqual(status,'unverified_card');self.assertEqual(proof['problems'],['OSError'])
        self.assertEqual(Advertisement.objects.count(),0)

    def test_explicit_empty_and_wrong_model_are_different_results(self):
        with patch('parsers.vehicle_audit.listing',return_value=[]):
            self.assertEqual(verify(None,self.model,'drom')[0],'empty')
        other={**self.ad,'brand':'audi','model':'a4','name':'Audi A4'}
        with patch('parsers.vehicle_audit.listing',return_value=[other]):
            self.assertEqual(verify(None,self.model,'drom')[0],'no_matching_model')

    def test_shared_lease_and_confirmed_record_survive_restarts(self):
        row=VehicleAudit.objects.create(model=self.model,source='drom',lease_until=timezone.now()+timedelta(minutes=1))
        with patch('parsers.vehicle_audit.verify',return_value=('confirmed',{'ad_url':self.ad['link']})) as check:
            process_audit(None,'drom');check.assert_not_called()
            row.lease_until=timezone.now()-timedelta(seconds=1);row.save()
            process_audit(None,'drom');process_audit(None,'drom');check.assert_called_once()
        row.refresh_from_db();self.assertEqual(row.status,'confirmed');self.assertTrue(row.checked_at)

    def test_block_pauses_the_whole_source_without_marking_other_models_checked(self):
        a=VehicleAudit.objects.create(model=self.model,source='avito')
        make=self.model.parent
        model=CatalogNode.objects.create(kind='model',parent=make,key='bmw/x3',slug='x3',name='X3')
        b=VehicleAudit.objects.create(model=model,source='avito')
        with patch('parsers.vehicle_audit.verify',side_effect=SourceBlocked('blocked')) as check:
            self.assertTrue(process_audit(None,'avito'))
            process_audit(None,'avito');check.assert_called_once()
        a.refresh_from_db();b.refresh_from_db()
        self.assertEqual(a.status,'blocked');self.assertEqual(b.status,'pending');self.assertIsNone(b.checked_at)

    def test_server_error_gives_short_audit_pause_without_an_access_block(self):
        from parsers.sources import SourceUnavailable
        from parsers.collector import saved_backoff_seconds
        row=VehicleAudit.objects.create(model=self.model,source='avito')
        with patch('parsers.vehicle_audit.verify',side_effect=SourceUnavailable('avito','captcha',500)) as check:
            self.assertFalse(process_audit(None,'avito'))
            process_audit(None,'avito');check.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.status,'error')
        self.assertEqual(row.evidence['error_reason'],'avito: captcha endpoint HTTP 500')
        self.assertLessEqual((row.pause_until-timezone.now()).total_seconds(),60)
        self.assertGreater((row.pause_until-timezone.now()).total_seconds(),55)
        # Ordinary users must not inherit a false access restriction after restart.
        self.assertEqual(saved_backoff_seconds('avito'),0)

    def test_active_new_search_defers_audit_without_spending_attempt(self):
        row=VehicleAudit.objects.create(model=self.model,source='drom')
        with patch('parsers.vehicle_audit.listing',return_value=[self.ad]),patch('parsers.vehicle_audit.urgent_search',return_value=True),patch('parsers.vehicle_audit.card') as card:
            process_audit(None,'drom');card.assert_not_called()
        row.refresh_from_db();self.assertEqual(row.status,'pending');self.assertEqual(row.attempts,0)

    def test_official_market_alias_search_uses_canonical_model(self):
        alias=CatalogNode.objects.create(kind='model',key='bmw/alias',slug='alias',name='Alias',parent=self.model.parent,
            data={'canonical_model_key':self.model.key})
        self.assertEqual(query_for(alias)['vehicle']['model']['key'],self.model.key)

    def test_changed_model_does_not_keep_a_foreign_generation(self):
        from parsers.collector import save_ad
        row=Advertisement.objects.create(ad_id='drom:12345678',platform='drom',name='BMW X3',brand='bmw',model='x3',price=1,
            generation_key='bmw/x3/old',generation_evidence={'model_key':'bmw/x3'})
        save_ad({**self.ad,'generation_key':'','generation_evidence':{'model_key':'bmw/x5','status':'unknown'}})
        row.refresh_from_db();self.assertEqual(row.generation_key,'');self.assertEqual(row.model,'x5')
        self.assertEqual(row.generation_evidence['model_key'],'bmw/x5')

    def test_report_contains_real_evidence_and_safe_csv(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import json,csv
        row=VehicleAudit.objects.create(model=self.model,source='drom',status='confirmed',
            evidence={'ad_url':self.ad['link'],'title':'=unsafe()','photo_checked':True})
        with TemporaryDirectory() as directory,patch('parsers.management.commands.audit_vehicles.RUNTIME_DIR',Path(directory)):
            call_command('audit_vehicles',report=True,stdout=StringIO())
            report=json.loads((Path(directory)/'reports/vehicle-audit.json').read_text())
            self.assertEqual(report['summary']['states'],{'confirmed':1})
            self.assertEqual(report['checks'][0]['ad_url'],self.ad['link'])
            with (Path(directory)/'reports/vehicle-audit.csv').open(encoding='utf-8-sig') as file:
                self.assertEqual(next(csv.DictReader(file))['title'],"'=unsafe()")
            with (Path(directory)/'reports/vehicle-audit-by-model.csv').open(encoding='utf-8-sig') as file:
                self.assertEqual(next(csv.DictReader(file))['drom_url'],self.ad['link'])

    def test_audit_cooldown_also_survives_a_collector_restart(self):
        from parsers.collector import saved_backoff_seconds
        VehicleAudit.objects.create(model=self.model,source='avito',status='blocked',pause_until=timezone.now()+timedelta(minutes=15))
        self.assertGreater(saved_backoff_seconds('avito'),895)
        self.assertEqual(saved_backoff_seconds('autoru'),0)

    def test_retry_failed_preserves_the_source_cooldown(self):
        pause=timezone.now()+timedelta(minutes=15)
        row=VehicleAudit.objects.create(model=self.model,source='avito',status='blocked',pause_until=pause)
        call_command('audit_vehicles',retry_failed=True,stdout=StringIO())
        with patch('parsers.vehicle_audit.verify') as verify:
            process_audit(None,'avito');verify.assert_not_called()
        row.refresh_from_db();self.assertEqual(row.pause_until,pause);self.assertEqual(row.status,'pending')

    def test_expired_inactive_search_does_not_starve_audit(self):
        from ads.models import SearchJob,ResultView
        from users.models import UserFilters,User
        from parsers.collector import active_search_jobs
        job=SearchJob.objects.create(fingerprint='inactive',filters={})
        UserFilters.objects.create(user_id=424242,search_job=job)
        self.assertIn(job,list(active_search_jobs()))
        job.requested_at=timezone.now()-timedelta(hours=2);job.save()
        self.assertNotIn(job,list(active_search_jobs()))
        User.objects.create(user_id=424242,monitoring_enabled=True)
        self.assertIn(job,list(active_search_jobs()))
        User.objects.filter(user_id=424242).update(monitoring_enabled=False)
        view=ResultView.objects.create(user_id=424242,chat_id=424242,message_id=999,filters={},search_job=job,
            collect_until=timezone.now()+timedelta(minutes=5))
        self.assertIn(job,list(active_search_jobs()))
        view.collect_until=timezone.now()-timedelta(seconds=1);view.save()
        self.assertNotIn(job,list(active_search_jobs()))

    def test_popular_scope_defers_others_and_preserves_evidence(self):
        popular=VehicleAudit.objects.create(model=self.model,source='drom',status='confirmed',evidence={'ad_url':self.ad['link']})
        other=CatalogNode.objects.create(kind='model',parent=self.model.parent,key='bmw/503',slug='503',name='503')
        deferred=VehicleAudit.objects.create(model=other,source='drom')
        with patch('parsers.management.commands.audit_vehicles.POPULAR_MODELS',{'bmw':('x5',)}):
            call_command('audit_vehicles',popular=True,prepare=True,stdout=StringIO())
        deferred.refresh_from_db();popular.refresh_from_db()
        self.assertEqual(deferred.status,'deferred');self.assertEqual(popular.evidence['ad_url'],self.ad['link'])
        self.assertEqual(VehicleAudit.objects.filter(model=self.model).count(),3)
        with patch('parsers.vehicle_audit.verify') as verify:
            process_audit(None,'drom');verify.assert_not_called()
        call_command('audit_vehicles',all_models=True,stdout=StringIO())
        deferred.refresh_from_db();self.assertEqual(deferred.status,'pending')

    def test_prepare_is_idempotent_and_never_changes_user_searches(self):
        from ads.models import SearchJob
        job=SearchJob.objects.create(fingerprint='personal',filters={'city':['Samara']})
        output=StringIO()
        call_command('audit_vehicles',prepare=True,stdout=output)
        call_command('audit_vehicles',prepare=True,stdout=output)
        self.assertEqual(VehicleAudit.objects.count(),3)
        job.refresh_from_db();self.assertEqual(job.filters,{'city':['Samara']})