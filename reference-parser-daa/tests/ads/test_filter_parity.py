"""Shared bot/web filters: real matching, validation, grammar and cache reuse."""
import json
from types import SimpleNamespace
from unittest.mock import patch
from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django.forms.models import model_to_dict
from ads.filter_schema import FILTER_OPTIONS, matches_attribute
from ads.forms import WebSearchForm
from ads.models import Advertisement, CatalogNode
from ads.query_language import parse_request
from ads.search import matches, results, snapshot
from users.models import UserFilters


class SourceAttributeTests(SimpleTestCase):
    def test_negative_condition_is_not_a_positive_substring_match(self):
        for actual in ('Не битый', 'Не требует ремонта', 'Без повреждений'):
            self.assertTrue(matches_attribute('condition', actual, ['не битый']))
            self.assertFalse(matches_attribute('condition', actual, ['битый']))
        self.assertTrue(matches_attribute('condition', 'Требует ремонта', ['битый']))
        self.assertFalse(matches_attribute('condition', 'Битый', ['не битый']))

    def test_each_source_attribute_spelling_has_the_same_meaning(self):
        for field, actual, wanted in (
            ('body_type','джип/suv 5 дв.','внедорожник'),
            ('body_type','Хэтчбек 5 дв.','хетчбек'),
            ('body_type','открытый','кабриолет'),
            ('gearbox','Автоматическая','автомат'),
            ('gearbox','АКПП','автомат'),
            ('gearbox','Роботизированная','робот'),
            ('drive','AWD','полный'),
            ('color','Чёрный металлик','черный'),
            ('color','Серебряный','серебристый'),
            ('seller','Официальный дилер','автодилер'),
            ('steering','Левый','левый'),
        ):
            with self.subTest(field=field, actual=actual):
                self.assertTrue(matches_attribute(field,actual,[wanted]))

    def test_unknown_attribute_cannot_satisfy_an_explicit_filter(self):
        for field, values in FILTER_OPTIONS.items():
            for actual in ('',None,'unknown','Неизвестно'):
                self.assertFalse(matches_attribute(field,actual,values))

    def test_multiple_alternatives_and_dual_fuel(self):
        self.assertTrue(matches_attribute('engine','бензин, газ',['газ']))
        self.assertTrue(matches_attribute('gearbox','MT',['автомат','механика']))
        self.assertFalse(matches_attribute('drive','задний',['полный','передний']))


class ExtendedGrammarTests(SimpleTestCase):
    def setUp(self):
        self.makes=[SimpleNamespace(name='BMW',slug='bmw')]

    def test_ownership_and_mileage_are_not_interpreted_as_budget(self):
        parsed=parse_request('бмв x5 пробег от 100 тыс до 200 тыс км до 2 владельцев до 4 млн',self.makes)
        self.assertEqual(parsed['model_text'],'x5')
        self.assertEqual(parsed['mileage'],'100000-200000')
        self.assertEqual(parsed['owners'],'-2')
        self.assertEqual(parsed['price'],'-4000000')

    def test_open_bounds_and_extended_categorical_filters(self):
        parsed=parse_request('бмв 3 2015 пробег от 150 тыс км 1 владелец черный левый руль не битый частник лифтбек газ',self.makes)
        self.assertEqual(parsed['model_text'],'3')
        self.assertEqual(parsed['mileage'],'150000-')
        self.assertEqual(parsed['owners'],'1-1')
        self.assertEqual(parsed['year'],'2015-2015')
        self.assertEqual(parsed['price'],'')
        for field, expected in dict(color=['черный'],steering=['левый'],condition=['не битый'],seller=['частное лицо'],body_type=['лифтбек'],engine=['газ']).items():
            self.assertEqual(parsed[field],expected)

    def test_series_name_is_not_mistaken_for_a_color(self):
        parsed=parse_request('бмв 3 серия серый до 1 млн',self.makes)
        self.assertEqual(parsed['model_text'],'3 серия')
        self.assertEqual(parsed['color'],['серый'])

    def test_invalid_mileage_and_ownership_bounds(self):
        for text in ('бмв x5 4-2 владельца', 'бмв x5 до 100 владельцев',
                     'бмв x5 пробег от 200 тыс до 100 тыс км', 'бмв x5 пробег до 11 млн км'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                parse_request(text,self.makes)


class FullFilterParityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.make=CatalogNode.objects.create(kind='make',key='bmw',slug='bmw',name='BMW',refreshed_at=timezone.now())
        self.model=CatalogNode.objects.create(kind='model',parent=self.make,key='bmw/x5',slug='x5',name='X5',refreshed_at=timezone.now())

    def form(self, **extra):
        return WebSearchForm({'make_id':self.make.pk,'model_id':self.model.pk,**extra})

    def car(self, **extra):
        index=Advertisement.objects.count()
        values=dict(ad_id=f'avito:{index}',platform='avito',name='BMW X5',brand='BMW',model='X5',city='Самара',price=1000000,year='2015')
        values.update(extra)
        return Advertisement.objects.create(**values)

    def test_web_and_bot_have_identical_complete_snapshots(self):
        form=self.form(price_min='500.000',price_max='2 млн',year_min=2010,year_max=2018,
            mileage_min='100 тыс',mileage_max='200 000',owners_min=1,owners_max=3,city='Самара +200',
            variant='30d',engine=['бензин','дизель'],gearbox=['автомат'],drive=['полный'],
            body_type=['внедорожник'],color=['чёрный','серебряный'],steering=['левый'],
            condition=['не битый'],seller=['частное лицо'],platform=['avito','drom'])
        self.assertTrue(form.is_valid(),form.errors)
        f=form.filters
        bot=UserFilters(user_id=918273,vehicle=self.model,brand=['bmw'],price='500000-2000000',
            year='2010-2018',mileage='100000-200000',owners='1-3',city=f['city'],location=f['location'],radius_km=200,
            variant='30d',engine=['бензин','дизель'],gearbox=['автомат'],drive=['полный'],body_type=['внедорожник'],
            color=['серебристый','черный'],steering=['левый'],condition=['не битый'],seller=['частное лицо'],platform=['avito','drom'])
        self.assertEqual(f,snapshot(bot))

    def test_zero_and_independent_lower_numeric_bounds(self):
        form=self.form(mileage_min='0',owners_min=0,price_max='400.000')
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.filters['mileage'],'0-')
        self.assertEqual(form.filters['owners'],'0-')
        self.assertEqual(form.filters['price'],'-400000')

    def test_custom_colors_are_normalized_and_unsafe_values_rejected(self):
        form=self.form(color=['черный'],color_custom='Чёрный, бордовый')
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.filters['color'],['бордовый','черный'])
        for value in ('<script>', 'red;alert', 'a', ','.join(['цвет']*9)):
            with self.subTest(value=value): self.assertFalse(self.form(color_custom=value).is_valid())

    def test_invalid_owner_and_mileage_ranges_never_enqueue_a_job(self):
        for extra in ({'owners_min':4,'owners_max':2},{'owners_min':-1},{'owners_max':100},
                      {'mileage_min':'200000','mileage_max':'100000'},{'mileage_max':'11 млн'}):
            response=self.client.post('/api/searches/',json.dumps({'make_id':self.make.pk,**extra}),content_type='application/json')
            self.assertEqual(response.status_code,400)
        from ads.models import SearchJob
        self.assertEqual(SearchJob.objects.count(),0)

    def test_all_bot_option_lists_are_shared_with_the_web(self):
        from bot.keyboards.inline.filters_mapping_kb import FILTER_OPTIONS as bot_options
        self.assertIs(bot_options,FILTER_OPTIONS)
        for field,values in FILTER_OPTIONS.items():
            form=self.form(**{field:list(values)})
            self.assertTrue(form.is_valid(),form.errors)
            self.assertEqual(set(form.filters[field]),set(values))

    def test_full_matching_excludes_unknown_and_rejects_damaged_cars(self):
        car=self.car(mileage=150000,owners='2',engine='дизель',gearbox='Автоматическая',drive='AWD',
            body_type='джип/suv 5 дв.',color='Чёрный',steering='Левый',condition='Не требует ремонта',seller='Частное лицо')
        form=self.form(mileage_min='100000',mileage_max='200000',owners_max=2,engine=['дизель'],gearbox=['автомат'],
            drive=['полный'],body_type=['внедорожник'],color=['черный'],steering=['левый'],condition=['не битый'],seller=['частное лицо'])
        self.assertTrue(form.is_valid(),form.errors)
        self.assertTrue(matches(model_to_dict(car),form.filters))
        car.condition='Битый'
        self.assertFalse(matches(model_to_dict(car),form.filters))
        car.condition='unknown'
        self.assertFalse(matches(model_to_dict(car),form.filters))

    def test_owners_are_prefiltered_numerically_without_casting_bad_source_data(self):
        good=self.car(owners='2')
        self.car(owners='4');self.car(owners='unknown');self.car(owners='9'*20)
        with patch('ads.search.matches',wraps=matches) as matcher:
            self.assertEqual(results({'owners':'1-3'}),[good])
            self.assertEqual(matcher.call_count,1)

    def test_sentence_saves_all_new_filters_in_telegram(self):
        from bot.handlers.text_search import save_choice
        extra=dict(owners='-2',mileage='100000-',color=['черный'],steering=['левый'],condition=['не битый'],seller=['частное лицо'])
        async_to_sync(save_choice)(918273,self.model.pk,'2015','',extra)
        stored=UserFilters.objects.get(user_id=918273)
        for field,value in extra.items(): self.assertEqual(getattr(stored,field),value)

    def test_sort_and_source_switch_reuse_one_matching_pass(self):
        a=self.car(price=1000000);b=self.car(price=500000);c=self.car(price=2000000,platform='drom')
        data=self.client.post('/api/searches/',json.dumps({'make_id':self.make.pk}),content_type='application/json').json()
        path='/api/searches/'+data['search_id']+'/'
        with patch('ads.web_search.results',side_effect=AssertionError('matching was repeated')):
            self.assertEqual(self.client.get(path+'?sort=cheap').json()['items'][0]['id'],b.pk)
            self.assertEqual(self.client.get(path+'?sort=expensive').json()['items'][0]['id'],c.pk)
            self.assertEqual(self.client.get(path+'?platform=drom&sort=cheap').json()['total'],1)

    def test_cached_membership_still_reads_updated_photos(self):
        car=self.car()
        data=self.client.post('/api/searches/',json.dumps({'make_id':self.make.pk}),content_type='application/json').json()
        car.images=['https://img.avito.st/image/fresh.jpg'];car.save(update_fields=['images'])
        refreshed=self.client.get('/api/searches/'+data['search_id']+'/').json()
        self.assertEqual(refreshed['items'][0]['photos'],car.images)

    def test_city_suggestions_cache_repeated_typo_search_without_exposing_coordinates(self):
        from ads.views import city_suggestions
        city_suggestions.cache_clear()
        a=self.client.get('/api/cities/?q=Самар').json()['options']
        self.assertEqual(a[0]['name'],'Самара')
        with patch('ads.views.cities',side_effect=AssertionError('directory rescanned')):
            b=self.client.get('/api/cities/?q=САМАР').json()['options']
        self.assertEqual(a,b)
        self.assertEqual(set(b[0]),{'id','name','region'})
