"""Shared city/radius parsing and the manual Telegram filter."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from asgiref.sync import async_to_sync
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from ads.geography import parse_city_input, find_city
from ads.query_language import parse_request
from ads.search import snapshot, results
from ads.models import Advertisement, CatalogNode, ResultView, SearchJob
from users.models import UserFilters
from bot.callbacks import filters_callback as handler
from bot.services.filters_service import update_user_location


class CityParsingTests(SimpleTestCase):
    def test_query_and_filter_share_aliases_radius_and_coordinates(self):
        for text in ('Самара +100','Самара+200 км','мск +200','СПБ +500 КМ','Самара'):
            with self.subTest(text=text):
                manual=parse_city_input(text)
                full=parse_request('bmw x5 '+text,[SimpleNamespace(name='BMW',slug='bmw')])
                for key in ('city','location','radius_km','city_choices'):
                    self.assertEqual(manual[key],full[key])

    def test_bad_input_is_not_silently_saved_or_broadened(self):
        for text in ('Самара +0','Самара +1001','Самара +100 +200','Самара +200.5',
                     'Самара Москва +200','+200','Неизвестноград +200','Самара от 500000'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                parse_city_input(text)

    def test_ambiguous_city_requires_region_choice(self):
        parsed=parse_city_input('Киров +200')
        self.assertGreater(len(parsed['city_choices']),1)
        self.assertEqual(parsed['location'],{})
        self.assertEqual(parsed['radius_km'],200)
        precise=parse_city_input('Киров Кировская +200')
        self.assertEqual(precise['location']['region'],'Кировская')


class CityFilterTests(TestCase):
    def setUp(self):
        make=CatalogNode.objects.create(key='bmw',kind='make',name='BMW',slug='bmw')
        self.job=SearchJob.objects.create(fingerprint='city-test')
        UserFilters.objects.create(user_id=7,vehicle=make,brand=['bmw'],year='2005-2010',price='500000-',
                                   generation_keys=['saved-generation'],search_job=self.job)
        self.state=FSMContext(storage=MemoryStorage(),key=StorageKey(bot_id=1,chat_id=-77,user_id=7))
        self.panel=SimpleNamespace(message_id=10,chat=SimpleNamespace(id=-77),answer=AsyncMock(),
                                   edit_text=AsyncMock(),edit_reply_markup=AsyncMock(),bot=AsyncMock())
        self.panel.answer.return_value=SimpleNamespace(message_id=20)
        self.message=SimpleNamespace(message_id=11,chat=self.panel.chat,text='Самара +200',
            from_user=SimpleNamespace(id=7),answer=AsyncMock(),bot=self.panel.bot)
        self.message.answer.return_value=SimpleNamespace(message_id=20)

    def cb(self,data,message=None):
        return SimpleNamespace(data=data,message=message or self.panel,from_user=SimpleNamespace(id=7),answer=AsyncMock())

    def test_open_filter_accepts_city_text_immediately_and_preserves_vehicle(self):
        async def flow():
            await handler.filter_handler(self.cb('filter_Город'),self.state)
            self.assertEqual(await self.state.get_state(),handler.FilterStates.entering_city.state)
            await handler.save_city_value(self.message,self.state)
            call=self.message.bot.edit_message_text.await_args.kwargs
            self.assertEqual((call['chat_id'],call['message_id']),(-77,10))
            self.assertIn('Самара +200',call['text'])
            buttons=[b for row in call['reply_markup'].inline_keyboard for b in row]
            self.assertTrue(any(b.callback_data=='city:radius:200' and b.text.startswith('✓') for b in buttons))
        async_to_sync(flow)()
        f=UserFilters.objects.get(user_id=7)
        self.assertEqual(f.city,['Самара']);self.assertEqual(f.radius_km,200)
        self.assertEqual(f.location['name'],'Самара')
        self.assertIsNotNone(f.vehicle_id);self.assertEqual(f.price,'500000-')
        self.assertEqual(f.year,'2005-2010');self.assertEqual(f.generation_keys,['saved-generation'])
        self.assertIsNone(f.search_job_id)

    def test_radius_buttons_then_exact_city_and_clearing(self):
        async def flow():
            await handler.filter_handler(self.cb('filter_Город'),self.state)
            await handler.toggle_city(self.cb('toggle_city_Самара'),self.state)
            await handler.city_action(self.cb('city:radius:100'),self.state)
        async_to_sync(flow)()
        f=UserFilters.objects.get(user_id=7);self.assertEqual(f.radius_km,100)
        self.message.text='+500 км'
        async_to_sync(handler.save_city_value)(self.message,self.state)
        f.refresh_from_db();self.assertEqual(f.radius_km,500)
        async_to_sync(handler.city_action)(self.cb('city:radius:0'),self.state)
        f.refresh_from_db();self.assertEqual(f.radius_km,0);self.assertEqual(f.city,['Самара'])
        async_to_sync(handler.city_action)(self.cb('city:radius:200'),self.state)
        async_to_sync(handler.clear_filter)(self.cb('clear_filter_Город'),self.state)
        f.refresh_from_db();self.assertEqual(f.radius_km,0);self.assertEqual(f.city,[]);self.assertEqual(f.location,{})

    def test_repeat_exact_city_button_is_a_safe_noop(self):
        async def flow():
            await handler.filter_handler(self.cb('filter_Город'),self.state)
            self.panel.edit_text.side_effect=TelegramBadRequest(method=None,message='message is not modified')
            await handler.city_action(self.cb('city:radius:0'),self.state)
        async_to_sync(flow)()
        self.assertEqual(UserFilters.objects.get(user_id=7).radius_km,0)
    def test_ambiguous_city_uses_same_lookup_and_saves_only_after_confirmation(self):
        async def flow():
            await handler.filter_handler(self.cb('filter_Город'),self.state)
            self.message.text='Киров +200'
            await handler.save_city_value(self.message,self.state)
            values=await self.state.get_data()
            self.assertGreater(len(values['city_choices']),1)
            choice=SimpleNamespace(message_id=20,chat=self.panel.chat,bot=self.panel.bot,
                                   edit_reply_markup=AsyncMock(),answer=AsyncMock())
            await handler.city_action(self.cb('city:choose:0',choice),self.state)
            choice.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
            await handler.city_action(self.cb('city:radius:100'),self.state)
            self.assertEqual((await self.state.get_data())['city_choices'],[])
            self.message.text='+200'
            await handler.save_city_value(self.message,self.state)
            self.assertEqual((await self.state.get_data())['city_choices'],[])
        async_to_sync(flow)()
        f=UserFilters.objects.get(user_id=7)
        self.assertEqual(f.city,['Киров']);self.assertEqual(f.radius_km,200)
        self.assertIn('latitude',f.location)

    def test_invalid_or_stale_input_does_not_modify_existing_filter(self):
        async_to_sync(update_user_location)(7,find_city('Самара')[0][0],100)
        async def flow():
            await handler.filter_handler(self.cb('filter_Город'),self.state)
            self.message.text='Самара +1001'
            await handler.save_city_value(self.message,self.state)
            stale=SimpleNamespace(message_id=999,chat=self.panel.chat)
            cb=self.cb('city:radius:200',stale)
            await handler.city_action(cb,self.state)
            self.assertTrue(cb.answer.await_args.kwargs['show_alert'])
        async_to_sync(flow)()
        f=UserFilters.objects.get(user_id=7);self.assertEqual(f.radius_km,100);self.assertEqual(f.city,['Самара'])

    def test_radius_reuses_shared_matching_and_native_avito_geography(self):
        from parsers.collector import collection_filters
        async_to_sync(update_user_location)(7,find_city('Самара')[0][0],200)
        f=UserFilters.objects.get(user_id=7)
        f.vehicle=None;f.brand=[];f.price='';f.year='';f.generation_keys=[];f.save()
        for i,city in enumerate(('Самара','Тольятти','Москва')):
            Advertisement.objects.create(ad_id=f'city:{i}',name='BMW X5',price=100,city=city)
        query=snapshot(f)
        self.assertEqual({a.city for a in results(query)},{'Самара','Тольятти'})
        for source in ('avito','autoru','drom'):
            collected=collection_filters(source,query)
            self.assertEqual(collected['city'],['Самара'] if source=='avito' else [])
            if source=='avito': self.assertEqual(collected['radius_km'],200)