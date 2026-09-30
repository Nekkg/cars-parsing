from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from datetime import timedelta
from ads.models import CatalogNode, SearchJob
from users.models import User, UserFilters
from ads.text_query import split_vehicle_query, model_suggestions
from ads.search import matches, snapshot
from parsers.sources import extract_listings


class QueryParserTests(SimpleTestCase):
    def setUp(self):
        self.make=SimpleNamespace(pk=1,name='BMW',slug='bmw',kind='make')
        self.models=[SimpleNamespace(pk=i,name=name,slug=slug,kind='model') for i,name,slug in
                     [(2,'3-Series','3-series'),(3,'X3','x3'),(4,'M3','m3')]]

    def test_modification_is_not_silently_broadened_to_whole_model(self):
        make,text,year=split_vehicle_query('bmw m340 2018',[self.make])
        rows,variant=model_suggestions(make,text,self.models)
        self.assertEqual([r.name for r in rows],['3-Series'])
        self.assertEqual((variant,year),('M340','2018'))
        self.assertTrue(matches({'name':'BMW M340i xDrive'},{'variant':variant}))
        self.assertFalse(matches({'name':'BMW 320i'},{'variant':variant}))

    def test_incomplete_query_and_russian_alias(self):
        make,text,year=split_vehicle_query('бмв 3',[self.make])
        rows,variant=model_suggestions(make,text,self.models)
        self.assertEqual(rows[0].name,'3-Series')
        self.assertEqual(year,'')
        rows,variant=model_suggestions(make,'m3',self.models)
        self.assertEqual(rows[0].name,'M3')
        self.assertEqual(variant,'')

    def test_empty_recommendations_are_end_not_listing_matches(self):
        self.assertEqual(extract_listings('<div class="ListingEmptyOffers"><a href="/sale/123456789/">Похожие предложения</a></div>','autoru'),[])
        with self.assertRaises(RuntimeError): extract_listings('<div>Unexpected HTML</div>','autoru')

    def test_keyboard_restores_notifications_and_removes_site(self):
        from bot.keyboards.reply.start_kb import start_keyboard
        from bot.keyboards.inline.filters_kb import filters_keyboard
        texts=[b.text for row in start_keyboard.keyboard for b in row]
        self.assertIn('Включить уведомления',texts)
        self.assertIn('Выключить уведомления',texts)
        self.assertIn('Очистить выбор',texts)
        self.assertNotIn('Смотреть все объявления',texts)
        self.assertTrue(start_keyboard.is_persistent)
        callbacks=[b.callback_data for row in filters_keyboard().inline_keyboard for b in row]
        self.assertIn('notifications:on',callbacks)
        self.assertIn('notifications:off',callbacks)
        self.assertIn('selection:clear',callbacks)

    async def test_single_model_query_asks_for_generation(self):
        from bot.handlers.text_search import query_message
        data={'models':[(2,'3-Series')],'make_id':1,'make_name':'BMW','year':'','variant':'','model_text':'3'}
        message=SimpleNamespace(text='bmw 3',from_user=SimpleNamespace(id=9))
        state=AsyncMock()
        with patch('bot.handlers.text_search.resolve_query',AsyncMock(return_value=data)),patch('bot.handlers.text_search.confirm_model',AsyncMock()) as confirm:
            await query_message(message,state)
            confirm.assert_awaited_once_with(message,state,9,2,data)

    async def test_generation_is_optional(self):
        from bot.handlers.text_search import query_callback
        callback=SimpleNamespace(answer=AsyncMock(),data='query:search',message=AsyncMock(),from_user=SimpleNamespace(id=9))
        state=AsyncMock();state.get_data.return_value={'query_model':2}
        with patch('bot.handlers.vehicle_search.launch_search',AsyncMock()) as launch:
            await query_callback(callback,state)
            launch.assert_awaited_once_with(callback.message,9,state)


class QueryPersistenceTests(TestCase):
    def setUp(self):
        self.make=CatalogNode.objects.create(kind='make',name='BMW',slug='bmw',key='bmw')
        self.model=CatalogNode.objects.create(kind='model',name='3-Series',slug='3-series',key='bmw/3-series',parent=self.make)

    def test_text_choice_replaces_stale_generation_and_exact_year(self):
        from bot.handlers.text_search import save_choice
        gen=CatalogNode.objects.create(kind='generation',name='Old',key='old',parent=self.model)
        UserFilters.objects.create(user_id=7,vehicle=gen,year='invalid',price='500000-')
        async_to_sync(save_choice)(7,self.model.pk,'2018','M340')
        filters=UserFilters.objects.get(user_id=7)
        self.assertEqual(filters.vehicle_id,self.model.pk)
        self.assertEqual(filters.year,'2018-2018')
        self.assertEqual(filters.price,'500000-')
        self.assertEqual(snapshot(filters)['variant'],'M340')

    def test_clear_removes_all_selection_and_disables_notifications(self):
        from bot.handlers.text_search import clear_selection
        User.objects.create(user_id=7,monitoring_enabled=True)
        UserFilters.objects.create(user_id=7,vehicle=self.model,year='2018-2018',variant='M340',price='500000-',city=['Самара'],sort_order='cheap')
        async_to_sync(clear_selection)(7)
        f=UserFilters.objects.get(user_id=7)
        self.assertIsNone(f.vehicle_id)
        self.assertFalse(f.variant)
        self.assertFalse(f.price)
        self.assertEqual(f.city,[])
        self.assertEqual(f.sort_order,'cheap')
        self.assertFalse(User.objects.get(user_id=7).monitoring_enabled)

    def test_source_backoff_survives_worker_restart(self):
        from run_collectors import saved_backoff_seconds
        SearchJob.objects.create(fingerprint='backoff',source_state={'avito':{'retry_after':(timezone.now()+timedelta(minutes=10)).isoformat()}})
        self.assertGreater(saved_backoff_seconds('avito'),590)
        self.assertEqual(saved_backoff_seconds('autoru'),0)
