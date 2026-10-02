from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import timedelta
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from selenium.common.exceptions import InvalidSessionIdException
from parsers.browser_session import RecoveringBrowser
from ads.search_status import source_responded, status_text
from ads.models import Advertisement, SearchJob
from ads.search import results, matches
from parsers.sources import extract_listings, enrich


class RecoveryTests(SimpleTestCase):
    def test_expired_get_retries_same_url_on_new_session(self):
        first=Mock(); second=Mock()
        first.get.side_effect=InvalidSessionIdException('Unable to find session')
        factory=Mock(side_effect=[first,second]); recovered=Mock()
        browser=RecoveringBrowser(factory,recovered)
        browser.get('https://www.avito.ru/samara/avtomobili')
        second.get.assert_called_once_with('https://www.avito.ru/samara/avtomobili')
        recovered.assert_called_once()
        self.assertEqual(factory.call_count,2)

    def test_expired_followup_restores_page_and_retries_once_only(self):
        first=Mock(); second=Mock()
        first.find_elements.side_effect=InvalidSessionIdException()
        second.find_elements.side_effect=InvalidSessionIdException()
        browser=RecoveringBrowser(Mock(side_effect=[first,second]))
        browser.get('https://example.com/page')
        with self.assertRaises(InvalidSessionIdException): browser.find_elements('css selector','a')
        second.get.assert_called_once_with('https://example.com/page')

    def test_status_never_exposes_selenium_internals(self):
        job=SimpleNamespace(filters={},source_state={'avito':{'status':'ошибка','error':'Unable to find session secret-id http://172.19.0.3:4444'}})
        value=status_text(job)
        self.assertNotIn('secret-id',value)
        self.assertNotIn('172.19',value)
        self.assertIn('повторно',value)

    def test_readiness_ignores_previous_search_and_accepts_block(self):
        self.assertFalse(source_responded({'cycle':'old','pages':9},'new'))
        self.assertFalse(source_responded({'cycle':'new','pages':0},'new'))
        self.assertTrue(source_responded({'cycle':'new','pages':1},'new'))
        self.assertTrue(source_responded({'attempt_at':'new','status':'доступ ограничен'},'new'))

    def test_avito_location_markers_and_detail_fallback(self):
        html='<div data-marker="item"><a data-marker="item-title" href="/samara/avtomobili/bmw_123456789">BMW X5, 2020</a><span data-marker="item-price">1000000</span><span data-marker="item-location">Самара</span></div>'
        self.assertEqual(extract_listings(html,'avito')[0]['city'],'Самара')
        ad=extract_listings(html.replace('data-marker="item-location"','class="old"'),'avito')[0]
        self.assertEqual(ad['city'],'')
        enrich(ad,'<div data-marker="item-view/item-address">Самара, улица</div>')
        self.assertIn('Самара',ad['city'])

    def test_removed_ad_type_does_not_hide_ads(self):
        self.assertTrue(matches({'ad_type':'unknown'},{'ad_type':['обычное']}))


class UnifiedResultsTests(TestCase):
    def test_sort_combines_all_sources_before_taking_three(self):
        items=[]
        for i,(source,price) in enumerate([('avito',300),('autoru',100),('drom',200),('drom',400)]):
            ad=Advertisement.objects.create(ad_id=str(i),platform=source,name='BMW',price=price)
            Advertisement.objects.filter(pk=ad.pk).update(created_at=timezone.now()-timedelta(days=i))
            items.append(ad)
        self.assertEqual([a.platform for a in results({})[:3]],['avito','autoru','drom'])
        self.assertEqual([a.price for a in results({},'cheap')[:3]],[100,200,300])
        self.assertEqual([a.price for a in results({},'expensive')[:3]],[400,300,200])
        self.assertEqual(len(results({'platform':['avito']},'cheap')),1)

    def test_missing_city_is_queued_for_detail_instead_of_discarded(self):
        from parsers.collector import collect
        job=SearchJob.objects.create(fingerprint='missingcity',filters={'city':['Самара']})
        html='<div data-ftid="bulls-list_bull"><a data-ftid="bull_title" href="https://auto.drom.ru/bmw/x5/123456789.html">BMW X5, 2020</a><span data-ftid="bull_price">1000000</span></div>'
        with patch('parsers.collector.load_page',return_value=html): collect(None,'drom',job)
        job.refresh_from_db()
        self.assertEqual(len(job.source_state['drom']['detail_queue']),1)
