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

    async def test_three_photo_cards_and_stable_next_page(self):
        from bot.handlers.vehicle_search import show_results
        message=AsyncMock(); state=AsyncMock()
        state.get_data.return_value={'result_ids':[1,2,3,4,5,6]}
        ads=[SimpleNamespace(pk=i) for i in [7,6,5,4,3,2,1]]
        with patch('bot.handlers.vehicle_search.UserFilters.objects.aget_or_create',AsyncMock(return_value=(SimpleNamespace(sort_order='newest',platform=[],search_job_id=None),False))), patch('bot.handlers.vehicle_search.current_results',AsyncMock(return_value=ads)), patch('bot.handlers.vehicle_search.user_description',AsyncMock(return_value='BMW')), patch('bot.handlers.vehicle_search.send_card',AsyncMock()) as send:
            await show_results(message,1,page=1,state=state)
            self.assertEqual([c.args[1].pk for c in send.await_args_list],[4,5,6])

    @patch('bot.handlers.vehicle_search.download_photo',return_value=b'jpeg-photo')
    async def test_photo_is_used_and_invalid_photo_falls_back(self, download):
        from bot.handlers.vehicle_search import send_card
        from aiogram.exceptions import TelegramBadRequest
        message=AsyncMock()
        ad=SimpleNamespace(name='BMW',price=100,year='2000',city='Самара',platform='drom',mileage=None,generation='',link='https://example.com/car',image='https://example.com/photo.jpg')
        await send_card(message,ad)
        message.answer_photo.assert_awaited_once()
        self.assertEqual(message.answer_photo.await_args.args[0].data,b'jpeg-photo')
        message.answer.assert_not_awaited()
        message.answer_photo.side_effect=TelegramBadRequest(method=SimpleNamespace(),message='invalid photo')
        await send_card(message,ad)
        message.answer.assert_awaited_once()
        self.assertIn('Фото сейчас недоступно',message.answer.await_args.args[0])


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

class FirstSourceBatchTests(SimpleTestCase):
    async def render(self, ads, page=0, data=None):
        from bot.handlers.vehicle_search import show_results
        message, state = AsyncMock(), AsyncMock()
        state.get_data.return_value = data or {}
        filters = SimpleNamespace(sort_order='newest',platform=[],search_job_id=None)
        with patch('bot.handlers.vehicle_search.UserFilters.objects.aget_or_create',AsyncMock(return_value=(filters,False))), patch('bot.handlers.vehicle_search.current_results',AsyncMock(return_value=ads)), patch('bot.handlers.vehicle_search.user_description',AsyncMock(return_value='Volvo S60')), patch('bot.handlers.vehicle_search.send_card',AsyncMock()) as send:
            await show_results(message,1,page=page,state=state)
        return message, state, send

    async def test_auto_batch_sends_exactly_one_from_each_source_in_fixed_order(self):
        ads = [SimpleNamespace(pk=i,platform=s) for i,s in enumerate(['drom','avito','drom','autoru','avito'],1)]
        message, state, send = await self.render(ads)
        self.assertEqual([c.args[1].platform for c in send.await_args_list],['avito','autoru','drom'])
        self.assertEqual(send.await_count,3)
        self.assertEqual(state.update_data.await_args.kwargs['result_first_size'],3)
        buttons = message.answer.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(sum(b.callback_data=='search:page:1' for row in buttons for b in row),1)

    async def test_unavailable_sources_do_not_fill_first_batch_with_duplicates(self):
        ads = [SimpleNamespace(pk=i,platform='drom') for i in range(1,4)]
        message, state, send = await self.render(ads)
        self.assertEqual(send.await_count,1)
        self.assertEqual(state.update_data.await_args.kwargs['result_first_size'],1)
        _, _, next_send = await self.render(ads,page=1,data={'result_ids':[1,2,3],'result_first_size':1})
        self.assertEqual([c.args[1].pk for c in next_send.await_args_list],[2,3])

    async def test_empty_results_have_no_next_batch_button(self):
        message, _, send = await self.render([])
        send.assert_not_awaited()
        buttons = message.answer.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertFalse(any(b.callback_data=='search:page:1' for row in buttons for b in row))

    def test_pending_generation_waits_for_first_verified_source_match(self):
        from ads.search_status import ready_for_batch
        pending = {'cycle':'token','pages':1,'status':'проверка карточек','detail_queue':[{}]}
        self.assertFalse(ready_for_batch(pending,'token',False))
        self.assertTrue(ready_for_batch(pending,'token',True))
        self.assertTrue(ready_for_batch({**pending,'status':'доступ ограничен'},'token',False))
        self.assertFalse(ready_for_batch({**pending,'status':'доступ ограничен'},'other-token',False))
