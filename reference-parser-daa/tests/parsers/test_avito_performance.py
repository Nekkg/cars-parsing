"""Regression checks for page progress and avoiding unnecessary Avito detail work."""
from datetime import timedelta
from unittest.mock import patch
from django.db import connection
from django.test import TestCase, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ads.models import SearchJob, Advertisement
from ads.search import results, matches
from parsers.collector import collect, pending_details, save_ad
from parsers.fetch_cache import fetch_key
from parsers.generations import GenerationIndex
from parsers.models import SourceFetch
from parsers.sources import extract_listings, SourceBlocked
from parsers.avito_client import listing_characteristics
from tests.parsers.test_generation_results import vehicle_catalog, card
from tests.parsers import test_avito_client as fixtures


def ad(number, **fields):
    row = card(identifier=str(123456789 + number))
    row['link'] = f"https://www.avito.ru/moskva/avtomobili/volvo_s60_{row['external_id']}"
    return {**row, **fields}


class AvitoQueuePerformanceTests(TestCase):
    def setUp(self):
        _, self.filters = vehicle_catalog()
        self.index = GenerationIndex.from_db()

    def warm(self, ads, details, *, stale=False, complete=True):
        expiry = timezone.now() + timedelta(seconds=-1 if stale else 3600)
        SourceFetch.objects.bulk_create([SourceFetch(key=fetch_key('avito', 'detail', row['link']),
            source='avito', expires_at=expiry,
            payload={'details': details, 'gallery_checked': complete}) for row in ads])

    def test_fifty_cached_matches_are_available_in_one_turn_not_fifty_detail_tasks(self):
        ads = [ad(i) for i in range(50)]
        self.warm(ads, {'generation': 'I (2000—2004)', 'year': '2003', 'price': 1})
        job = SearchJob.objects.create(fingerprint='warm-page', filters=self.filters)
        payload = {'ads': ads, 'saved_count': 50, 'new_count': 50, 'has_next': True}
        with CaptureQueriesContext(connection) as queries, \
             patch('parsers.collector.fetch_listing_page', return_value=(payload, False)), \
             patch('parsers.collector.fetch_ad_details') as details:
            collect(None, 'avito', job)
        details.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.source_state['avito']['detail_queue'], [])
        self.assertEqual(len(results(job.filters)), 50)
        self.assertTrue(all(row.price == 500000 for row in results(job.filters)))
        # One cache SELECT serves the whole page.
        self.assertEqual(sum('FROM "parsers_sourcefetch"' in row['sql'] for row in queries), 1)

    def test_checked_unknown_generations_do_not_repeat_or_become_year_only_matches(self):
        ads = [ad(i) for i in range(8)]
        self.warm(ads, {'year': '2003'})
        self.assertEqual(pending_details(ads, self.filters, self.index), [])
        self.assertFalse(any(matches(row, self.filters) for row in ads))

    def test_expired_or_incomplete_cache_does_not_hide_a_needed_detail(self):
        for i, stale, complete in ((0, True, True), (1, False, False)):
            row = ad(i)
            self.warm([row], {'year': '2003'}, stale=stale, complete=complete)
            self.assertEqual(pending_details([row], self.filters, self.index), [row])

    def test_known_nonmatching_attributes_are_skipped_but_unknowns_still_need_cards(self):
        filters = {**self.filters, 'mileage': '-150000', 'owners': '-2', 'engine': ['бензин']}
        ads = [ad(0, mileage=200000), ad(1, owners='3'), ad(2, engine='дизель'), ad(3)]
        self.assertEqual(pending_details(ads, filters, self.index), [ads[-1]])

    def test_unknown_mileage_alone_queues_details(self):
        job = SearchJob.objects.create(fingerprint='missing-mileage', filters={'mileage': '-150000'})
        payload = {'ads': [ad(0)], 'saved_count': 1, 'has_next': False}
        with patch('parsers.collector.fetch_listing_page', return_value=(payload, False)):
            collect(None, 'avito', job)
        job.refresh_from_db()
        self.assertEqual(len(job.source_state['avito']['detail_queue']), 1)

    def test_next_page_loads_after_three_cards_instead_of_waiting_for_all_fifty(self):
        job = SearchJob.objects.create(fingerprint='interleave', filters={'engine': ['бензин']})
        def listing(driver, source, url, page, **kwargs):
            ads = [ad(i) for i in range((page - 1) * 50, page * 50)]
            for row in ads: save_ad(row)
            return {'ads': ads, 'saved_count': 50, 'new_count': 50, 'has_next': True}, False
        def detail(driver, source, row, index):
            row['engine'] = 'бензин'
            save_ad(row)
            return row
        with patch('parsers.collector.fetch_listing_page', side_effect=listing) as pages, \
             patch('parsers.collector.fetch_ad_details', side_effect=detail) as details:
            for _ in range(3): collect(None, 'avito', job)
        self.assertEqual([call.args[3] for call in pages.call_args_list], [1, 2])
        self.assertEqual(details.call_count, 3)
        job.refresh_from_db()
        self.assertEqual(len(job.source_state['avito']['detail_queue']), 97)
        self.assertEqual(Advertisement.objects.filter(platform='avito').count(), 100)
        self.assertEqual(len(results(job.filters)), 3)

    def test_overlapping_listing_pages_do_not_duplicate_pending_cards(self):
        job = SearchJob.objects.create(fingerprint='overlap', filters={'engine': ['бензин']})
        def listing(driver, source, url, page, **kwargs):
            offset = 0 if page == 1 else 10
            ads = [ad(i) for i in range(offset, offset + 50)]
            for row in ads: save_ad(row)
            return {'ads': ads, 'saved_count': 50, 'new_count': 50, 'has_next': True}, False
        def detail(driver, source, row, index):
            row['engine'] = 'бензин'
            save_ad(row)
        with patch('parsers.collector.fetch_listing_page', side_effect=listing), \
             patch('parsers.collector.fetch_ad_details', side_effect=detail):
            for _ in range(3): collect(None, 'avito', job)
        job.refresh_from_db()
        queue = job.source_state['avito']['detail_queue']
        self.assertEqual(len(queue), 57)
        self.assertEqual(len({row['external_id'] for row in queue}), 57)

    def test_large_backlog_is_drained_before_requesting_another_page(self):
        job = SearchJob.objects.create(fingerprint='bounded', filters={'engine': ['бензин']})
        job.source_state = {'avito': {'cycle': job.requested_at.isoformat(), 'detail_queue': [ad(i) for i in range(150)],
            'listing_finished': False, 'prefer_listing': True, 'next_page': 4}}
        job.save(update_fields=['source_state'])
        with patch('parsers.collector.fetch_listing_page') as pages, patch('parsers.collector.fetch_ad_details') as details:
            collect(None, 'avito', job)
        pages.assert_not_called()
        self.assertEqual(details.call_count, 3)
        job.refresh_from_db()
        self.assertEqual(len(job.source_state['avito']['detail_queue']), 147)

    def test_block_on_the_next_listing_preserves_detail_queue_and_cursor(self):
        job = SearchJob.objects.create(fingerprint='block-page', filters={'engine': ['бензин']})
        job.source_state = {'avito': {'cycle': job.requested_at.isoformat(), 'detail_queue': [ad(i) for i in range(10)],
            'listing_finished': False, 'prefer_listing': True, 'next_page': 2, 'route': 0, 'count': 50}}
        job.save(update_fields=['source_state'])
        with patch('parsers.collector.fetch_listing_page', side_effect=SourceBlocked('blocked')):
            with self.assertRaises(SourceBlocked): collect(None, 'avito', job)
        job.refresh_from_db()
        self.assertEqual(len(job.source_state['avito']['detail_queue']), 10)
        self.assertEqual(job.source_state['avito']['next_page'], 2)
        self.assertEqual(job.source_state['avito']['count'], 50)


class AvitoListingCharacteristicTests(SimpleTestCase):
    def test_explicit_title_gearbox_is_available_without_opening_the_car(self):
        for badge, wanted in (('AT', 'автомат'), ('MT', 'механика'), ('AMT', 'робот'), ('CVT', 'вариатор')):
            self.assertEqual(listing_characteristics(f'BMW X5 2.0 {badge}, 2018')['gearbox'], wanted)
        self.assertEqual(listing_characteristics('BMW X5, 2018'), {})
        self.assertEqual(listing_characteristics('MT Cars, 2018'), {})

    def test_ssr_merges_only_matching_dom_card_parameters_not_seller_prose(self):
        fixture = fixtures.AvitoAdapterTests()
        row = fixture.item()
        row['title'] = 'BMW X5 3.0 AT, 2020, 45 000 км'
        dom = (f'<div data-marker="item"><a data-marker="item-title" href="{row["urlPath"]}">BMW X5</a>'
               '<div data-marker="item-specific-params">внедорожник 5 дв., полный, бензин, газ</div>'
               '<div data-marker="item-description">дизель, механика, поколение G05</div></div>')
        ads = extract_listings(fixture.page([row]) + dom, 'avito')
        self.assertEqual(ads[0]['gearbox'], 'автомат')
        self.assertEqual(ads[0]['engine'], 'бензин, газ')
        self.assertEqual(ads[0]['drive'], 'полный')
        self.assertEqual(ads[0]['body_type'], 'внедорожник')
        self.assertEqual(ads[0]['generation'], '')

    def test_dom_fallback_also_supplies_explicit_characteristics(self):
        page = ('<div data-marker="item"><a data-marker="item-title" href="/samara/avtomobili/volvo_s60_123456789">Volvo S60 2.0 MT, 2003</a>'
                '<span data-marker="item-price">500000</span><div data-marker="item-specific-params">седан, передний, дизель</div></div>')
        row = extract_listings(page, 'avito')[0]
        self.assertEqual((row['gearbox'], row['engine'], row['drive'], row['body_type']),
                         ('механика', 'дизель', 'передний', 'седан'))
