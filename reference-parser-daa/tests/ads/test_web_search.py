"""Web integrations: shared normalization, bounded collection, partial results, isolation."""
import json
from datetime import timedelta
from unittest.mock import patch
from django.core.cache import cache
from django.test import TestCase, Client
from django.utils import timezone
from ads.forms import WebSearchForm
from ads.models import Advertisement, CatalogNode, SearchJob, WebSearch
from ads.search import matches, results, snapshot
from ads.geography import find_city
from users.models import UserFilters


class WebSearchTests(TestCase):
    def setUp(self):
        cache.clear()
        now = timezone.now()
        self.make = CatalogNode.objects.create(kind='make', key='bmw', slug='bmw', name='BMW', refreshed_at=now)
        self.model = CatalogNode.objects.create(kind='model', parent=self.make, key='bmw/3-series', slug='3-series', name='3-Series', refreshed_at=now)
        self.gen1 = CatalogNode.objects.create(kind='generation', parent=self.model, key='bmw/3-series/e90', slug='e90', name='5 поколение', data={'year_from':2005,'year_to':2012,'codes':['E90']}, refreshed_at=now)
        self.gen2 = CatalogNode.objects.create(kind='generation', parent=self.model, key='bmw/3-series/f30', slug='f30', name='6 поколение', data={'year_from':2011,'year_to':2019,'codes':['F30']}, refreshed_at=now)

    def create(self, client=None, **extra):
        return (client or self.client).post('/api/searches/', json.dumps({'make_id':self.make.pk,'model_id':self.model.pk,**extra}), content_type='application/json')

    def car(self, source='avito', city='Самара', **extra):
        index=Advertisement.objects.count()
        return Advertisement.objects.create(ad_id=f'{source}:{index}', platform=source,
            name='BMW 3-Series', brand='BMW', model='3-Series', city=city, price=extra.pop('price',1000000),
            year=extra.pop('year','2011'), link=f'https://'+{'avito':'www.avito.ru','autoru':'auto.ru','drom':'auto.drom.ru'}[source]+f'/cars/{index}/', **extra)

    def test_home_does_not_enqueue_or_scan_advertisements(self):
        with self.assertNumQueries(0):
            response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Радиус, км')
        self.assertEqual(SearchJob.objects.count(),0)

    def test_price_open_bounds_city_alias_and_radius_share_bot_snapshot(self):
        form=WebSearchForm({'make_id':self.make.pk,'model_id':self.model.pk,'price_min':'500.000','city':'самара +200','year_max':2012})
        self.assertTrue(form.is_valid(),form.errors)
        filters=form.filters
        self.assertEqual(filters['price'],'500000-')
        self.assertEqual(filters['year'],'-2012')
        self.assertEqual(filters['radius_km'],200)
        self.assertEqual(filters['location']['name'],'Самара')
        bot=UserFilters(user_id=9,vehicle=self.model,price='500000-',year='-2012',city=filters['city'],location=filters['location'],radius_km=200,brand=['bmw'])
        self.assertEqual(filters,snapshot(bot))
        for text,name in [('мск','Москва'),('спб','Санкт-Петербург')]:
            form=WebSearchForm({'make_id':self.make.pk,'city':text})
            self.assertTrue(form.is_valid(),form.errors)
            self.assertEqual(form.filters['location']['name'],name)

    def test_all_sources_partial_updates_and_radius(self):
        nearby=self.car('avito','Тольятти'); self.car('drom','Москва')
        response=self.create(city='Самара +200',price_min='500 000')
        self.assertEqual(response.status_code,201,response.content)
        data=response.json(); path='/api/searches/'+data['search_id']+'/'
        self.assertEqual(data['counts'],{'avito':1,'autoru':0,'drom':0})
        self.assertTrue(data['collecting'])
        self.assertEqual(data['items'][0]['id'],nearby.pk)
        self.car('autoru'); self.car('drom','Тольятти')
        cache.clear()
        updated=self.client.get(path).json()
        self.assertEqual(updated['total_all'],3)
        self.assertEqual(updated['counts'],{'avito':1,'autoru':1,'drom':1})
        self.assertEqual(updated['items'][0]['platform'],'drom')

    def test_web_search_is_picked_by_worker_and_lease_expires(self):
        from parsers.collector import active_search_jobs
        data=self.create().json(); search=WebSearch.objects.get(pk=data['search_id'])
        self.assertIn(search.search_job_id,list(active_search_jobs().values_list('pk',flat=True)))
        search.collect_until=timezone.now()-timedelta(seconds=1);search.save()
        self.assertNotIn(search.search_job_id,list(active_search_jobs().values_list('pk',flat=True)))
        self.assertFalse(self.client.get('/api/searches/'+str(search.pk)+'/').json()['collecting'])
        self.assertEqual(self.client.post('/api/searches/'+str(search.pk)+'/refresh/').status_code,200)
        search.refresh_from_db(); self.assertGreater(search.collect_until,timezone.now())

    def test_two_browsers_share_job_but_cannot_read_or_refresh_other_view(self):
        other=Client(); first=self.create().json(); second=self.create(other).json()
        self.assertNotEqual(first['search_id'],second['search_id'])
        self.assertEqual(SearchJob.objects.count(),1)
        path='/api/searches/'+first['search_id']+'/'
        self.assertEqual(other.get(path).status_code,404)
        self.assertEqual(other.post(path+'refresh/').status_code,404)
        self.assertEqual(Client().get(path).status_code,404)
        self.assertEqual(other.get('/api/searches/'+second['search_id']+'/').status_code,200)

    def test_new_search_stops_old_view_only_for_current_session(self):
        other=Client(); first=self.create().json(); foreign=self.create(other).json(); self.create(year_min=2015)
        old=WebSearch.objects.get(pk=first['search_id']); untouched=WebSearch.objects.get(pk=foreign['search_id'])
        self.assertLessEqual(old.collect_until,timezone.now())
        self.assertGreater(untouched.collect_until,timezone.now())

    def test_generations_are_multiple_and_model_scoped(self):
        self.car(generation_key=self.gen1.key); self.car('drom',year='2015',generation_key=self.gen2.key); self.car('autoru',generation_key='')
        response=self.create(generation_ids=[self.gen1.pk,self.gen2.pk])
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.json()['total'],2)
        form=WebSearchForm({'make_id':self.make.pk,'generation_ids':[self.gen1.pk]})
        self.assertFalse(form.is_valid()); self.assertIn('generation_ids',form.errors)
        model=CatalogNode.objects.create(kind='model',parent=self.make,key='bmw/x5',slug='x5',name='X5')
        self.assertEqual(self.create(model_id=model.pk,generation_ids=[self.gen1.pk]).status_code,400)

    def test_invalid_constraints_are_rejected_before_queueing(self):
        cases=[{'price_min':'2 млн','price_max':'1 млн'},{'radius_km':200},{'city':'нет такого города'},
               {'year_min':2020,'year_max':2018},{'year_min':'2000abc'},{'platform':['unknown']},
               {'city_id':'invented','city':'Самара'},{'generation_ids':'1,2'}, {'engine':['magic']}, {'generation_ids':[True]}, {'generation_ids':[1.5]}]
        for case in cases:
            with self.subTest(case=case): self.assertEqual(self.create(**case).status_code,400)
        self.assertEqual(SearchJob.objects.count(),0)

    def test_csrf_and_post_are_required_to_start_collection(self):
        secure=Client(enforce_csrf_checks=True)
        self.assertEqual(self.create(secure).status_code,403)
        self.assertEqual(self.client.get('/api/searches/').status_code,405)
        response=secure.get('/'); token=response.cookies['csrftoken'].value
        response=secure.post('/api/searches/',json.dumps({'make_id':self.make.pk}),content_type='application/json',HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code,201)

    def test_sorting_platform_tabs_pagination_and_cache(self):
        for index in range(22): self.car('avito',price=1000000+index)
        other=self.car('drom',price=500000)
        created=self.create().json(); path='/api/searches/'+created['search_id']+'/'
        self.assertEqual(len(created['items']),18)
        self.assertEqual(created['pages'],2)
        self.assertEqual(len(self.client.get(path+'?page=2').json()['items']),5)
        self.assertEqual(self.client.get(path+'?sort=cheap').json()['items'][0]['id'],other.pk)
        filtered=self.client.get(path+'?platform=drom').json()
        self.assertEqual(filtered['total'],1);self.assertEqual(filtered['total_all'],23)
        self.assertEqual(SearchJob.objects.count(),1)
        with patch('ads.web_search.results',side_effect=AssertionError('cached results rescanned')):
            self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.client.get(path+'?sort=invalid').status_code,400)

    def test_status_and_urls_do_not_expose_internal_data(self):
        car=self.car(image='https://127.0.0.1/private',images=['https://img.avito.st/image/abc.jpg'])
        car.link='javascript:alert(1)';car.save()
        data=self.create().json(); search=WebSearch.objects.get(pk=data['search_id']); job=search.search_job
        job.source_state={'avito':{'status':'доступ ограничен','error_code':'blocked','error':'PRIVATE proxy password', 'detail_queue':[{'cookies':'PRIVATE'}], 'attempt_at':job.requested_at.isoformat()}}
        job.save();response=self.client.get('/api/searches/'+str(search.pk)+'/')
        self.assertNotContains(response,'PRIVATE')
        self.assertEqual(response.json()['items'][0]['url'],'')
        self.assertEqual(response.json()['items'][0]['photos'],['https://img.avito.st/image/abc.jpg'])
        self.assertEqual(self.client.get('/ad/missing/').status_code,404)
        self.assertEqual(self.client.get('/ad/'+car.ad_id+'/').status_code,200)

    def test_catalogue_russian_search_and_city_autocomplete(self):
        with patch('ads.views.load_children',return_value=[self.make]):
            self.assertEqual(self.client.get('/catalog/?q=бмв').json()['options'][0]['id'],self.make.pk)
        city=self.client.get('/api/cities/?q=мск').json()['options'][0]
        self.assertEqual(city['name'],'Москва')
        self.assertIn('region',city)
        self.assertEqual(self.client.get('/catalog/?parent=not-an-id').status_code,400)

    def test_sentence_parser_reuses_bot_resolution(self):
        response=self.client.post('/api/query/',json.dumps({'text':'бмв 3 2010–2012 самара +200 до 1,2 млн'}),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        data=response.json()
        self.assertEqual(data['models'][0]['id'],self.model.pk)
        self.assertEqual(data['extra']['price'],'-1200000')
        self.assertEqual(data['extra']['radius_km'],200)
        self.assertEqual(data['year'],'2010-2012')

    def test_numeric_prefilter_reduces_python_matching_and_handles_unknown(self):
        matching=self.car(year='2011',mileage=100000)
        self.car(year='unknown');self.car(year='99999999999999999999');self.car(year='2020');self.car(year='2011',mileage=300000)
        with patch('ads.search.matches',wraps=matches) as matcher:
            self.assertEqual(results({'year':'2010-2012','mileage':'-150000'}),[matching])
            self.assertEqual(matcher.call_count,1)
