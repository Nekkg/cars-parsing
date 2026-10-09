"""Integration checks for the Russian examples, independent queries and true radius."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from ads.models import Advertisement, CatalogNode
from ads.query_language import parse_request, money
from ads.geography import find_city, within_radius
from ads.search import snapshot, matches, results
from users.models import UserFilters


class RussianGrammarTests(SimpleTestCase):
    def setUp(self):
        self.makes = [SimpleNamespace(name='BMW',slug='bmw'),SimpleNamespace(name='Mercedes-Benz',slug='mercedes-benz')]

    def test_four_screenshot_examples(self):
        cases = [('камри 2018–2020 калининград','camry','2018-2020','','Калининград',0),
                 ('бмв g20 москва до 4 млн','g20','','-4000000','Москва',0),
                 ('мерс c200 до 2015 спб','c200','-2015','','Санкт-Петербург',0),
                 ('солярис 2017–2020 до 1.2 млн казань +200','solaris','2017-2020','-1200000','Казань',200)]
        for text,model,year,price,city,radius in cases:
            with self.subTest(text=text):
                value = parse_request(text,self.makes)
                self.assertEqual((value['year'],value['price'],value['city'][0],value['radius_km']),(year,price,city,radius))
                self.assertTrue(value['model_text'])

    def test_money_separators_and_short_suffixes(self):
        for text,expected in [('500 000',500000),('400.000',400000),('1,2 млн',1200000),
                              ('4 млн.',4000000),('150 тыс',150000),('1.2м',1200000),('500к',500000)]:
            self.assertEqual(money(text),expected)

    def test_independent_year_price_bounds_and_specs(self):
        value = parse_request('бмв 3 от 2010 до 2015 от 500 000 до 1 млн дизель автомат полный привод пробег до 150 тыс км авито',self.makes)
        self.assertEqual(value['year'],'2010-2015')
        self.assertEqual(value['price'],'500000-1000000')
        self.assertEqual(value['model_text'],'3')
        self.assertEqual(value['mileage'],'-150000')
        self.assertEqual(value['engine'],['дизель'])
        self.assertEqual(value['drive'],['полный'])
        self.assertEqual(value['gearbox'],['автомат'])
        self.assertEqual(value['platform'],['avito'])

    def test_cheap_four_digit_ruble_sum_is_not_a_year(self):
        value = parse_request('бмв 3 до 2015 руб.',self.makes)
        self.assertEqual(value['price'],'-2015')
        self.assertEqual(value['year'],'')

    def test_invalid_constraints_are_rejected(self):
        for text in ('бмв 3 2020-2018','бмв 3 от 2 млн до 1 млн','бмв 3 +200',
                     'бмв 3 самара +1001','бмв 3 самара москва','бмв 3 до 1.5 руб.',
                     'бмв 3 до 1 млн до 2 млн'):
            with self.subTest(text=text),self.assertRaises(ValueError): parse_request(text,self.makes)

    def test_real_radius_and_unknown_place(self):
        origin = find_city('Самара')[0][0]
        self.assertTrue(within_radius('Тольятти',origin,200))
        self.assertTrue(within_radius('Самарская область, Самара',origin,200))
        self.assertFalse(within_radius('Москва',origin,200))
        self.assertFalse(within_radius('Неизвестная деревня',origin,200))
        self.assertFalse(within_radius('',origin,200))

    def test_collection_does_not_limit_radius_to_origin_city(self):
        from parsers.collector import collection_filters
        for source in ('autoru','drom'):
            self.assertEqual(collection_filters(source,{'city':['Самара'],'radius_km':200})['city'],[])
        avito=collection_filters('avito',{'city':['Самара'],'radius_km':200})
        self.assertEqual(avito['city'],['Самара'])
        self.assertEqual(avito['radius_km'],200)


class RussianQueryIntegrationTests(TestCase):
    def setUp(self):
        def make(slug,name): return CatalogNode.objects.create(kind='make',slug=slug,key=slug,name=name,refreshed_at=timezone.now())
        def model(parent,slug,name): return CatalogNode.objects.create(kind='model',slug=slug,key=parent.key+'/'+slug,name=name,parent=parent,refreshed_at=timezone.now())
        self.bmw = make('bmw','BMW')
        self.three = model(self.bmw,'3-series','3-Series')
        self.merc = make('mercedes-benz','Mercedes-Benz')
        self.cclass = model(self.merc,'c-class','C-Class')
        self.camry = model(make('toyota','Toyota'),'camry','Camry')
        self.solaris = model(make('hyundai','Hyundai'),'solaris','Solaris')
        self.g20 = CatalogNode.objects.create(kind='generation',key='g20',parent=self.three,
            data={'codes':['G20','G21'],'generation_number':7},refreshed_at=timezone.now())
        self.g20rest = CatalogNode.objects.create(kind='generation',key='g20-rest',parent=self.three,
            data={'codes':['G20'],'generation_number':7},refreshed_at=timezone.now())
        self.other = CatalogNode.objects.create(kind='generation',key='f30',parent=self.three,
            data={'codes':['F30'],'generation_number':6},refreshed_at=timezone.now())

    def test_model_only_aliases_and_modification_and_code(self):
        from bot.handlers.text_search import resolve_query
        for text,model in [('камри 2018–2020 калининград',self.camry),
                           ('бмв g20 москва до 4 млн',self.three),
                           ('мерс c200 до 2015 спб',self.cclass),
                           ('солярис 2017–2020 до 1.2 млн казань +200',self.solaris)]:
            data = async_to_sync(resolve_query)(text)
            self.assertEqual(data['models'][0][0],model.pk)
        self.assertEqual(async_to_sync(resolve_query)('мерс c200')['variant'],'C200')
        self.assertEqual(async_to_sync(resolve_query)('бмв g20')['extra']['generation_hint'],'G20')

    def test_new_sentence_resets_stale_constraints_and_keeps_all_new_ones(self):
        from bot.handlers.text_search import resolve_query, save_choice
        UserFilters.objects.create(user_id=7,vehicle=self.cclass,price='5000000-',engine=['дизель'])
        data = async_to_sync(resolve_query)('солярис 2017–2020 до 1.2 млн казань +200')
        async_to_sync(save_choice)(7,self.solaris.pk,data['year'],data['variant'],data['extra'])
        filters = UserFilters.objects.get(user_id=7)
        self.assertEqual(filters.price,'-1200000')
        self.assertEqual(filters.year,'2017-2020')
        self.assertEqual(filters.radius_km,200)
        self.assertEqual(filters.engine,[])
        self.assertEqual(filters.location['name'],'Казань')

    def test_chassis_family_includes_facelift_and_rejects_other_generations(self):
        from bot.handlers.text_search import resolve_query, save_choice
        data = async_to_sync(resolve_query)('бмв g20 москва до 4 млн')
        async_to_sync(save_choice)(7,self.three.pk,data['year'],data['variant'],data['extra'])
        filters = snapshot(UserFilters.objects.get(user_id=7))
        self.assertEqual(set(filters['generation_keys']),{'g20','g20-rest'})
        for key in ('g20','g20-rest','f30',''):
            row = {'name':'BMW 3-Series','brand':'BMW','model':'3-Series','generation_key':key,'city':'Москва','price':3000000}
            self.assertEqual(matches(row,filters),key.startswith('g20'))

    def test_null_ordinal_does_not_include_unrelated_generations(self):
        self.g20.data = {'codes':['G20']};self.g20.save()
        self.g20rest.data = {'codes':['OTHER']};self.g20rest.save()
        filters = UserFilters.objects.create(user_id=7,vehicle=self.three,generation_hint='G20')
        self.assertEqual(snapshot(filters)['generation_keys'],['g20'])

    def test_real_radius_results_include_neighbour_city(self):
        for i,city in enumerate(('Самара','Тольятти','Москва','Неизвестно')):
            Advertisement.objects.create(ad_id=str(i),platform='drom',name='Toyota Camry',price=100,city=city)
        filters = {'city':['Самара'],'location':find_city('Самара')[0][0],'radius_km':200}
        self.assertEqual({ad.city for ad in results(filters)},{'Самара','Тольятти'})

    async def test_complete_query_starts_once_incomplete_asks_generation(self):
        from bot.handlers.text_search import confirm_model
        message,state = AsyncMock(),AsyncMock()
        data = {'year':'2018-2020','variant':'','extra':{'year':'2018-2020'}}
        with patch('bot.handlers.text_search.save_choice',AsyncMock()),patch('bot.handlers.vehicle_search.launch_search',AsyncMock()) as launch:
            await confirm_model(message,state,7,self.three.pk,data)
            launch.assert_awaited_once_with(message,7,state)
            message.answer.assert_not_awaited()
        with patch('bot.handlers.text_search.save_choice',AsyncMock()),patch('bot.handlers.vehicle_search.user_description',AsyncMock(return_value='BMW 3-Series')),patch('bot.handlers.vehicle_search.launch_search',AsyncMock()) as launch:
            await confirm_model(message,state,7,self.three.pk,{'year':'','variant':'','extra':{}})
            launch.assert_not_awaited()
            self.assertIn('Выбрать поколение',message.answer.await_args.args[0])
    def test_cyrillic_codes_and_partial_russian_names(self):
        from bot.handlers.text_search import resolve_query
        for query,expected in [('бмв г20',self.three),('мерс с200',self.cclass),('тойота камр',self.camry)]:
            data = async_to_sync(resolve_query)(query)
            self.assertEqual(data['models'][0][0],expected.pk)
        self.assertTrue(async_to_sync(resolve_query)('тойота камр 2018')['needs_confirmation'])

    def test_manual_city_changes_and_clear_remove_previous_radius(self):
        from bot.services.filters_service import update_user_filter, clear_user_filter
        filters = UserFilters.objects.create(user_id=7,city=['Самара'],location=find_city('Самара')[0][0],radius_km=200)
        async_to_sync(update_user_filter)(7,'city',['Москва'])
        filters.refresh_from_db()
        self.assertEqual((filters.radius_km,filters.location),(0,{}))
        filters.radius_km=100;filters.location=find_city('Москва')[0][0];filters.save()
        async_to_sync(clear_user_filter)(7,'city')
        filters.refresh_from_db()
        self.assertEqual((filters.city,filters.radius_km,filters.location),([],0,{}))
