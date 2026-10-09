"""Search periods must narrow upstream queries without replacing body identity."""
import base64
import json
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import parse_qs,urlparse
from django.test import SimpleTestCase,TestCase
from ads.models import CatalogNode,SearchJob
from ads.search import snapshot
from ads.vehicle_selection import effective_year_bounds
from users.models import UserFilters
from parsers.collector import collection_filters,collect,pending_details
from parsers.generations import GenerationIndex
from parsers.query_filters import numeric_params,hydrate_generation_years,avito_year_filter
from parsers.sources import source_url


def filters():
    return {'vehicle':{'make':{'key':'volvo','slug':'volvo','name':'Volvo'},
        'model':{'key':'volvo/s60','slug':'s60','name':'S60'},
        'generations':[{'key':'volvo/s60/i','year_from':2000,'year_to':2004}]}}


class YearQueryTests(SimpleTestCase):
    def test_generation_years_and_explicit_bounds_intersect_in_supported_native_filters(self):
        data={**filters(),'year':'2002-2020','price':'-900000'}
        self.assertEqual(effective_year_bounds(data),(2002,2004))
        self.assertEqual(numeric_params(data,'drom'),{'maxprice':900000,'minyear':2002,'maxyear':2004})
        self.assertEqual(numeric_params(data,'autoru'),{'price_to':900000,'year_from':2002,'year_to':2004})
        self.assertEqual(numeric_params(data,'avito')['pmax'],900000)
        token = numeric_params(data,'avito')['f']
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4))
        self.assertEqual(json.loads(raw[raw.index(b'{'):]), {'from':2002,'to':2004})
        for source, names in (('drom', ('minyear', 'maxyear')), ('autoru', ('year_from', 'year_to'))):
            query = parse_qs(urlparse(source_url(source, data, page=3)).query)
            self.assertEqual([query[name] for name in names], [['2002'], ['2004']])
            self.assertNotIn('generation', query)

    def test_shared_avito_query_keeps_personal_constraints_local(self):
        one={**filters(),'price':'-1000000','engine':['бензин']}
        two=deepcopy(one);two['price']='-5000000';two['engine']=['дизель']
        two['vehicle']['generations'][0]['key']='another-catalogue-key'
        original=deepcopy(one)
        a=source_url('avito',collection_filters('avito',one),page=2)
        b=source_url('avito',collection_filters('avito',two),page=2)
        self.assertEqual(a,b);self.assertEqual(one,original)
        query=parse_qs(urlparse(a).query)
        self.assertEqual(query['q'],['Volvo S60']);self.assertEqual(query['p'],['2'])
        self.assertEqual(query['f'], ['ASgCAgECAUX6jBQXeyJmcm9tIjoyMDAwLCJ0byI6MjAwNH0'])
        self.assertNotIn('pmax',query)

    def test_avito_token_exactly_matches_year_only_ui_url_without_make_or_model(self):
        # External fixture: public Avito UI URL supplied by the user, 2026-10-05.
        self.assertEqual(avito_year_filter(2000,2004),
                         'ASgCAgECAUX6jBQXeyJmcm9tIjoyMDAwLCJ0byI6MjAwNH0')
        for lo,hi,want in ((2000,None,{'from':2000}),(None,2004,{'to':2004})):
            token=avito_year_filter(lo,hi)
            raw=base64.urlsafe_b64decode(token+'='*(-len(token)%4))
            self.assertEqual(json.loads(raw[raw.index(b'{'):]),want)
        self.assertIsNone(avito_year_filter(None,None))
        for pair in ((2020,2000),(-1,2000),(2000,10000),('2000',2004),(True,2004)):
            with self.assertRaises(ValueError):avito_year_filter(*pair)

    def test_open_periods_and_multiple_generations_are_conservative(self):
        data=filters();data['vehicle']['generations'].append({'key':'ii','year_from':2010,'year_to':None})
        self.assertEqual(effective_year_bounds(data),(2000,None))
        self.assertEqual(numeric_params(data,'drom'),{'minyear':2000})
        data['year']='2001-2023'
        self.assertEqual(effective_year_bounds(data),(2001,2023))
        self.assertEqual(numeric_params({},'avito'),{})

    def test_explicit_years_can_exclude_one_generation_or_fall_entirely_in_a_gap(self):
        data = filters()
        data['vehicle']['generations'].append({'key': 'ii', 'year_from': 2010, 'year_to': 2015})
        data['year'] = '2003-2008'
        self.assertEqual(effective_year_bounds(data), (2003, 2004))
        data['year'] = '2006-2008'
        lo, hi = effective_year_bounds(data)
        self.assertGreater(lo, hi)
        for source in ('avito', 'autoru', 'drom'):
            with self.assertRaises(ValueError):
                source_url(source, data)

    def test_conflicting_periods_never_form_an_ignored_inverted_query(self):
        data={**filters(),'year':'2020-2023'}
        for source in ('avito','autoru','drom'):
            with self.assertRaises(ValueError):source_url(source,data)


class YearCollectionTests(TestCase):
    def setUp(self):
        make=CatalogNode.objects.create(kind='make',key='volvo',slug='volvo',name='Volvo')
        self.model=CatalogNode.objects.create(kind='model',parent=make,key='volvo/s60',slug='s60',name='S60')
        self.base=CatalogNode.objects.create(kind='generation',parent=self.model,key='volvo/s60/i',
            name='I P24',data={'year_from':2000,'year_to':2004,'codes':['P24'],'generation_number':1})
        self.rest=CatalogNode.objects.create(kind='generation',parent=self.model,key='volvo/s60/rest',
            name='I рестайлинг P24',data={'year_from':2004,'year_to':2010,'codes':['P24'],'generation_number':1})

    def test_chassis_hint_supplies_years_without_changing_generation_selection(self):
        user=UserFilters.objects.create(user_id=12345,vehicle=self.model,generation_hint='P24')
        data=snapshot(user)
        self.assertNotIn('generations',data['vehicle'])
        self.assertEqual(set(data['generation_keys']),{self.base.key,self.rest.key})
        self.assertEqual(numeric_params(data,'drom'),{'minyear':2000,'maxyear':2010})

    def test_old_snapshot_hydration_does_not_change_its_identity_or_include_other_models(self):
        data=filters();data['vehicle'].pop('generations');data['generation_keys']=[self.base.key]
        other=CatalogNode.objects.create(kind='model',parent=self.model.parent,key='volvo/v60',name='V60',slug='v60')
        foreign=CatalogNode.objects.create(kind='generation',parent=other,key='volvo/v60/i',data={'year_from':2010})
        data['generation_keys'].append(foreign.key)
        original=deepcopy(data);hydrated=hydrate_generation_years(data)
        self.assertEqual(data,original);self.assertNotIn('generations',hydrated['vehicle'])
        self.assertEqual([row['key'] for row in hydrated['generation_years']],[self.base.key])
        self.assertEqual(effective_year_bounds(hydrated),(2000,2004))

    def test_nonoverlapping_year_and_generation_finish_without_source_requests(self):
        data={**filters(),'year':'2020-2023'}
        job=SearchJob.objects.create(fingerprint='impossible-years',filters=data)
        with patch('parsers.collector.fetch_listing_page') as request:
            for source in ('avito','autoru','drom'):collect(None,source,job)
            request.assert_not_called()
        job.refresh_from_db()
        for state in job.source_state.values():
            self.assertEqual(state['status'],'готово');self.assertEqual(state['count'],0)
            self.assertEqual(state['requested_at'],job.requested_at.isoformat())

    def test_old_cursor_restarts_with_first_native_year_filtered_page_on_each_source(self):
        for source in ('avito','autoru','drom'):
            job=SearchJob.objects.create(fingerprint='old-year-cursor-'+source,filters=filters())
            job.source_state={source:{'cycle':job.requested_at.isoformat(),'next_page':7,'route':0,
                'count':300,'pages':6,'fingerprints':[]}}
            job.save(update_fields=['source_state'])
            with patch('parsers.collector.fetch_listing_page',return_value=({'ads':[],'saved_count':0,'has_next':False},False)) as request:
                collect(None,source,job)
            self.assertEqual(request.call_args.args[3],1)
            job.refresh_from_db();self.assertEqual(job.source_state[source]['query_years'],[2000,2004])

    def test_cursor_for_the_current_year_window_continues_without_restarting(self):
        job=SearchJob.objects.create(fingerprint='current-year-cursor',filters=filters())
        job.source_state={'avito':{'cycle':job.requested_at.isoformat(),'next_page':7,'route':0,
            'count':300,'pages':6,'fingerprints':[],'query_years':[2000,2004],'query_years_version':2}}
        job.save(update_fields=['source_state'])
        with patch('parsers.collector.fetch_listing_page',return_value=({'ads':[],'saved_count':0,'has_next':False},False)) as request:
            collect(None,'avito',job)
        self.assertEqual(request.call_args.args[3],7)

    def test_finishing_old_details_still_starts_the_new_year_filtered_stream(self):
        ad={'platform':'avito','external_id':'123456789','brand':'volvo','model':'s60',
            'link':'https://www.avito.ru/samara/avtomobili/volvo_s60_123456789',
            'name':'Volvo S60','price':600000,'city':'Самара','year':'2002','generation':''}
        job=SearchJob.objects.create(fingerprint='old-finished-details',filters=filters())
        job.source_state={'avito':{'cycle':job.requested_at.isoformat(),'next_page':8,'route':1,
            'count':300,'pages':7,'fingerprints':[],'listing_finished':True,'detail_queue':[ad]}}
        job.save(update_fields=['source_state'])
        payload={'ads':[],'saved_count':0,'has_next':False}
        with patch('parsers.collector.fetch_ad_details') as detail, patch('parsers.collector.fetch_listing_page',return_value=(payload,False)) as request:
            collect(None,'avito',job)
            detail.assert_not_called()
            self.assertEqual(request.call_args.args[3],1)
            self.assertIn('f',parse_qs(urlparse(request.call_args.args[2]).query))
            job.refresh_from_db()
            self.assertIsNone(job.source_state['avito']['requested_at'])
            self.assertEqual(len(job.source_state['avito']['detail_queue']),1)
            collect(None,'avito',job)
            detail.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.source_state['avito']['detail_queue'],[])

    def test_unknown_year_is_checked_in_card_known_out_of_period_year_is_skipped(self):
        data={**filters(),'year':'2002-2003'}
        candidate={'platform':'avito','external_id':'123456789','brand':'volvo','model':'s60',
            'link':'https://www.avito.ru/samara/avtomobili/volvo_s60_123456789','name':'Volvo S60','price':600000,'city':'Самара','year':None,'generation':''}
        old={**candidate,'external_id':'123456780','year':'2000'}
        self.assertEqual(pending_details([candidate,old],data,GenerationIndex.from_db()),[candidate])
class AvitoCanonicalYearTests(SimpleTestCase):
    def test_make_model_years_match_complete_ui_url_from_user(self):
        # Both independent public URLs were supplied by the user.
        actual = avito_year_filter(2000, 2004, facets='ASgBAgICAkTgtg26mSjitg2eryg')
        self.assertEqual(actual, 'ASgBAgECAkTgtg26mSjitg2erygBRfqMFBd7ImZyb20iOjIwMDAsInRvIjoyMDA0fQ')

    def test_redirect_retains_years_pagination_sort_price_and_radius(self):
        from parsers.query_filters import avito_redirect_url
        original = ('https://www.avito.ru/samara/avtomobili?q=Volvo+S60&p=3&s=104&pmax=900000'
                    '&radius=200&f=' + avito_year_filter(2000, 2004))
        target = 'https://www.avito.ru/samara/avtomobili/volvo/s60-ASgBAgICAkTgtg26mSjitg2eryg?context=public'
        result = avito_redirect_url(original, target)
        query = parse_qs(urlparse(result).query)
        self.assertEqual(query['p'], ['3'])
        self.assertEqual(query['s'], ['104'])
        self.assertEqual(query['pmax'], ['900000'])
        self.assertEqual(query['radius'], ['200'])
        self.assertNotIn('context', query)
        self.assertNotIn('q', query)
        self.assertEqual(query['f'], [avito_year_filter(2000, 2004, facets='ASgBAgICAkTgtg26mSjitg2eryg')])

    def test_unknown_canonical_schema_never_silently_drops_years(self):
        from parsers.query_filters import avito_redirect_url
        original = 'https://www.avito.ru/all/avtomobili?f=' + avito_year_filter(2000, 2004)
        for target in ('https://www.avito.ru/all/avtomobili/volvo/s60-ASgUnknown',
                       'https://www.avito.ru/all/avtomobili/volvo/s60'):
            with self.assertRaises(RuntimeError):
                avito_redirect_url(original, target)

    def test_redirect_without_facets_keeps_generic_filter(self):
        from parsers.query_filters import avito_redirect_url
        original = 'https://www.avito.ru/all/avtomobili?p=2&f=' + avito_year_filter(2000, None)
        result = avito_redirect_url(original, 'https://www.avito.ru/rossiya/avtomobili')
        self.assertEqual(parse_qs(urlparse(result).query)['f'], [avito_year_filter(2000, None)])

class AvitoRedirectRegressionTests(SimpleTestCase):
    def test_hyphenated_model_does_not_become_part_of_the_filter_token(self):
        from parsers.query_filters import avito_redirect_url
        source = 'https://www.avito.ru/all/avtomobili?f=' + avito_year_filter(2000, 2004)
        target = 'https://www.avito.ru/all/avtomobili/nissan/x-trail-ASgBAgICAkTgtg26mSjitg2eryg?context=stale'
        result = avito_redirect_url(source, target)
        query = parse_qs(urlparse(result).query)
        self.assertNotIn('context', query)
        self.assertEqual(query['f'], [avito_year_filter(2000,2004,facets='ASgBAgICAkTgtg26mSjitg2eryg')])


class AvitoLegacyYearCursorTests(TestCase):
    def test_old_year_cursor_restarts_after_canonical_filter_fix(self):
        job = SearchJob.objects.create(fingerprint='old-canonical-cursor', filters=filters())
        job.source_state = {'avito':{'cycle':job.requested_at.isoformat(),'next_page':7,'route':0,
            'pages':6,'count':300,'query_years':[2000,2004],'query_years_version':1}}
        job.save(update_fields=['source_state'])
        with patch('parsers.collector.fetch_listing_page',return_value=({'ads':[],'saved_count':0,'has_next':False},False)) as request:
            collect(None,'avito',job)
        self.assertEqual(request.call_args.args[3],1)
        job.refresh_from_db()
        self.assertEqual(job.source_state['avito']['query_years_version'],2)

class AvitoLegacyBacklogPriorityTests(TestCase):
    def test_new_filtered_page_precedes_large_legacy_queue_without_losing_old_cards(self):
        from unittest.mock import Mock
        old = [{'external_id':str(123456700+i)} for i in range(139)]
        fresh = [old[30], {'external_id':'999999999'}]
        job = SearchJob.objects.create(fingerprint='legacy-backlog-priority',filters=filters())
        job.source_state = {'avito':{'cycle':job.requested_at.isoformat(),'next_page':12,'route':0,
            'pages':11,'count':550,'query_years':[2000,2004],'query_years_version':1,
            'detail_queue':old,'listing_finished':False}}
        job.save(update_fields=['source_state'])
        payload = {'ads':fresh,'saved_count':2,'new_count':1,'has_next':True}
        with patch('parsers.collector.pending_details',side_effect=[old,fresh]), \
             patch('parsers.collector.fetch_listing_page',return_value=(payload,False)) as listing, \
             patch('parsers.collector.fetch_ad_details') as detail:
            collect(Mock(),'avito',job)
        detail.assert_not_called()
        self.assertEqual(listing.call_args.args[3],1)
        job.refresh_from_db()
        queue = job.source_state['avito']['detail_queue']
        self.assertEqual(queue[:2],fresh)
        self.assertEqual(len(queue),140)
        self.assertEqual({ad['external_id'] for ad in queue}, {ad['external_id'] for ad in old+fresh})
