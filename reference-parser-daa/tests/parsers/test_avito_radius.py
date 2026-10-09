"""Avito radius is native upstream and old nationwide cursors restart safely."""
from unittest.mock import patch
from urllib.parse import parse_qs,urlparse
from django.test import SimpleTestCase,TestCase
from ads.models import SearchJob
from parsers.collector import collect,collection_filters
from parsers.sources import source_url
from parsers.query_filters import avito_redirect_url


def filters():
    return {'vehicle':{'make':{'key':'bmw','slug':'bmw','name':'BMW'},
            'model':{'key':'bmw/x5','slug':'x5','name':'X5'}},'year':'2015-2018','city':['Самара'],'radius_km':200}


class NativeRadiusTests(SimpleTestCase):
    def test_supported_city_has_native_radius_but_personal_prices_remain_local(self):
        url=source_url('avito',collection_filters('avito',{**filters(),'price':'-4000000'}))
        parsed=urlparse(url);query=parse_qs(parsed.query)
        self.assertEqual(parsed.path,'/samara/avtomobili')
        self.assertEqual(query['radius'],['200'])
        self.assertEqual(query['searchRadius'],['200'])
        self.assertEqual(query['localPriority'],['1'])
        self.assertNotIn('pmax',query)
        self.assertIn('f',query)

    def test_unsupported_city_is_not_silently_limited_to_a_wrong_origin(self):
        url=source_url('avito',collection_filters('avito',{**filters(),'city':['Небольшой город']}))
        parsed=urlparse(url)
        self.assertEqual(parsed.path,'/all/avtomobili')
        self.assertNotIn('radius',parse_qs(parsed.query))

    def test_canonical_redirect_retains_both_radius_parameters(self):
        original=source_url('avito',collection_filters('avito',filters()),page=3)
        target='https://www.avito.ru/samara/avtomobili/bmw/x5-ASgBAgICAkTgtg26mSjitg2eryg'
        query=parse_qs(urlparse(avito_redirect_url(original,target)).query)
        self.assertEqual(query['radius'],['200'])
        self.assertEqual(query['searchRadius'],['200'])
        self.assertEqual(query['p'],['3'])


class RadiusCursorTests(TestCase):
    def job(self, location=None):
        job=SearchJob.objects.create(fingerprint='radius-cursor',filters=filters())
        state={'cycle':job.requested_at.isoformat(),'next_page':9,'route':0,'count':400,'pages':8,
               'fingerprints':[],'query_years':[2015,2018],'query_years_version':2,'listing_finished':False}
        if location is not None:state['query_location']=location
        job.source_state={'avito':state};job.save(update_fields=['source_state'])
        return job

    def test_legacy_nationwide_cursor_restarts_first_native_page(self):
        job=self.job()
        with patch('parsers.collector.fetch_listing_page',return_value=({'ads':[],'saved_count':0,'has_next':False},False)) as request:
            collect(None,'avito',job)
        self.assertEqual(request.call_args.args[3],1)
        self.assertIn('/samara/',request.call_args.args[2])
        job.refresh_from_db()
        self.assertEqual(job.source_state['avito']['query_location'],['samara',200])
        self.assertEqual(job.source_state['avito']['count'],0)

    def test_current_native_radius_cursor_continues_the_next_page(self):
        job=self.job(['samara',200])
        with patch('parsers.collector.fetch_listing_page',return_value=({'ads':[],'saved_count':0,'has_next':False},False)) as request:
            collect(None,'avito',job)
        self.assertEqual(request.call_args.args[3],9)

    def test_geo_migration_prioritizes_first_page_and_keeps_queued_cards(self):
        old=[{'external_id':'123456789'}]
        job=self.job();job.source_state['avito']['detail_queue']=old;job.save(update_fields=['source_state'])
        with patch('parsers.collector.pending_details',side_effect=[old,[]]), \
             patch('parsers.collector.fetch_ad_details') as detail, \
             patch('parsers.collector.fetch_listing_page',return_value=({'ads':[],'saved_count':0,'has_next':False},False)) as request:
            collect(None,'avito',job)
        detail.assert_not_called();self.assertEqual(request.call_args.args[3],1)
        job.refresh_from_db();self.assertEqual(job.source_state['avito']['detail_queue'],old)
