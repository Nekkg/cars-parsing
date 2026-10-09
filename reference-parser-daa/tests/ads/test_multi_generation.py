from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from asgiref.sync import async_to_sync
from django.test import TestCase, SimpleTestCase
from ads.models import Advertisement, CatalogNode
from ads.search import matches, results, snapshot
from ads.filter_values import describe
from users.models import UserFilters
from parsers.query_filters import numeric_params
from parsers.collector import pending_details, save_ad
from parsers.generations import GenerationIndex
from parsers.sources import extract_listings, source_url, enrich


class MultipleGenerationsTests(TestCase):
    def setUp(self):
        self.make = CatalogNode.objects.create(kind='make', key='lexus', name='Lexus', slug='lexus')
        self.model = CatalogNode.objects.create(kind='model', key='lexus/is250', name='IS250', slug='is250', parent=self.make)
        self.other = CatalogNode.objects.create(kind='model', key='lexus/is350', name='IS350', slug='is350', parent=self.make)
        self.base = self.gen('base', '2 поколение', 2005, 2008, 2, 0)
        self.rest = self.gen('rest', '2 поколение, рестайлинг', 2008, 2010, 2, 1)
        self.third = self.gen('third', '3 поколение', 2013, 2016, 3, 0)
        self.filters = UserFilters.objects.create(user_id=123, vehicle=self.model)
        self.index = GenerationIndex.from_db()

    def gen(self, slug, desc, start, end, number, phase):
        return CatalogNode.objects.create(kind='generation', key=self.model.key+'/'+slug,
            parent=self.model, slug=slug, name=desc, data={'description':desc, 'codes':['XE20' if number==2 else 'XE30'],
            'year_from':start,'year_to':end,'generation_number':number,'facelift_number':phase})

    def ad(self, name='Lexus IS 250 II, 2007', identifier='123456789', year='2007', model='is', source='autoru'):
        return {'name':name,'brand':'lexus','model':model,'platform':source,'external_id':identifier,
                'link':f'https://auto.ru/cars/used/sale/lexus/is/{identifier}-ab/', 'price':1200000,
                'year':year,'generation':'','generation_codes':[],'city':'Самара','image':''}

    def test_or_selection_and_sql_results_exclude_other_generation(self):
        self.filters.generation_keys = [self.base.key, self.third.key]
        self.filters.save()
        f = snapshot(self.filters)
        for a in [self.ad(), self.ad('Lexus IS 250 II Рестайлинг, 2009','123456790','2009'),
                  self.ad('Lexus IS 250 III, 2014','123456791','2014')]:
            self.index.normalize(a);save_ad(a)
        self.assertEqual({row.year for row in results(f)}, {'2007','2014'})
        self.assertEqual(len(f['vehicle']['generations']),2)
        self.assertNotIn('generation',f['vehicle'])
        self.assertIn('2 поколение (XE20); 3 поколение (XE30)',describe(f))
        self.assertEqual(numeric_params(f,'autoru'),{'year_from':2005,'year_to':2016})
        gap = self.ad('Lexus IS 250, 2011',year='2011',model='is250')
        self.assertFalse(matches(gap,f))
        self.assertEqual(pending_details([gap],f,self.index),[])

    def test_selection_confirmation_validates_model_and_can_clear(self):
        from bot.handlers.vehicle_search import save_generations, select_vehicle
        async_to_sync(save_generations)(123,self.model.pk,[self.base.key,self.rest.key])
        self.filters.refresh_from_db()
        self.assertEqual(len(self.filters.generation_keys),2)
        saved = deepcopy(snapshot(self.filters))
        with self.assertRaises(ValueError):
            async_to_sync(save_generations)(123,self.other.pk,[self.base.key])
        async_to_sync(save_generations)(123,self.model.pk,[])
        self.filters.refresh_from_db()
        self.assertNotIn('generations',snapshot(self.filters)['vehicle'])
        self.assertEqual(len(saved['vehicle']['generations']),2)
        async_to_sync(save_generations)(123,self.model.pk,[self.base.key])
        async_to_sync(select_vehicle)(123,self.other.pk)
        self.filters.refresh_from_db()
        self.assertEqual(self.filters.generation_keys,[])

    def test_legacy_single_selection_and_stale_keys_are_restrictive(self):
        self.filters.vehicle=self.base;self.filters.save()
        f=snapshot(self.filters)
        self.assertIn('generation',f['vehicle'])
        ad=self.ad();self.index.normalize(ad)
        self.assertTrue(matches(ad,f))
        self.filters.vehicle=self.model;self.filters.generation_keys=['missing'];self.filters.save()
        self.assertFalse(matches(ad,snapshot(self.filters)))
        foreign=self.ad(model='rx')
        self.index.normalize(foreign)
        self.assertEqual(foreign['model'],'rx')
        self.assertFalse(matches(foreign,snapshot(self.filters)))
        self.assertEqual(results(snapshot(self.filters)),[])

    def test_lexus_route_and_title_mapping(self):
        f=snapshot(self.filters)
        self.assertIn('/cars/lexus/is/all/',source_url('autoru',f))
        from urllib.parse import parse_qs, urlsplit
        self.assertEqual(parse_qs(urlsplit(source_url('avito',f)).query)['q'],['Lexus IS'])
        ad=self.ad();self.index.normalize(ad)
        self.assertEqual(ad['model'],'is250')
        self.assertEqual(ad['generation_key'],self.base.key)
        other=self.ad('Lexus IS 350 II, 2007',model='is')
        self.index.normalize(other)
        self.assertEqual(other['model'],'is350')
        self.assertFalse(matches(other,f))
        unknown=self.ad('Lexus IS, 2007',model='is')
        self.index.normalize(unknown)
        self.assertFalse(matches(unknown,f))
        self.assertEqual(pending_details([unknown],f,self.index),[unknown])

    def test_avito_engine_badge_and_details_generation(self):
        f=snapshot(self.filters);f['vehicle']['generation']={'key':self.base.key,'year_from':2005,'year_to':2008}
        ad=self.ad('Lexus IS 2.5 AT, 2007, 160 000 км',model='IS',source='avito')
        self.index.normalize(ad)
        self.assertEqual(ad['model'],'is250')
        self.assertEqual(pending_details([ad],f,self.index),[ad])
        html='<div data-marker="item-view/item-params"><li>Модель: IS</li><li>Поколение: II (2005—2008)</li><li>Тип двигателя: Бензин</li></div>'
        enrich(ad,html,self.index)
        self.assertTrue(matches(ad,f))
        for engine, name in [('гибрид, 2.5 л','Lexus IS 2.5 AT, 2014'),('бензин, 3.5 л','Lexus IS, 2007')]:
            other=self.ad(name,source='avito');other['engine']=engine
            self.index.normalize(other)
            self.assertFalse(matches(other,f))
        convertible=self.ad('Lexus IS 2.5 AT, 2007',source='avito');convertible['body_type']='кабриолет'
        self.index.normalize(convertible)
        self.assertFalse(matches(convertible,f))

    def test_catalog_alias_selection_and_new_text_query_reset(self):
        from bot.handlers.vehicle_search import save_generations, saved_generations
        from bot.handlers.text_search import save_choice
        alias = CatalogNode.objects.create(kind='model',key='lexus/alias',slug='alias',name='Alias',parent=self.make,
                                           data={'canonical_model_key':self.model.key})
        self.filters.vehicle=alias;self.filters.save()
        async_to_sync(save_generations)(123,self.model.pk,[self.base.key])
        self.filters.refresh_from_db()
        self.assertEqual(self.filters.vehicle_id,self.model.pk)
        self.assertEqual(async_to_sync(saved_generations)(123,self.model.pk),[self.base.key])
        async_to_sync(save_choice)(123,self.model.pk,'','')
        self.filters.refresh_from_db()
        self.assertEqual(self.filters.generation_keys,[])

    def test_city_and_restylings_are_separate_constraints(self):
        ad=self.ad('Lexus IS 250 II Рестайлинг, 2008',year='2008');self.index.normalize(ad);save_ad(ad)
        self.filters.vehicle=self.base;self.filters.city=['Самара'];self.filters.save()
        self.assertEqual(results(snapshot(self.filters)),[])
        self.filters.vehicle=self.model;self.filters.generation_keys=[self.base.key,self.rest.key];self.filters.save()
        self.assertEqual(len(results(snapshot(self.filters))),1)

    def test_normalization_never_overrides_explicit_different_badge(self):
        ad=self.ad(model='is350')
        self.index.normalize(ad)
        self.assertEqual(ad['model'],'is350')
        self.assertFalse(matches(ad,snapshot(self.filters)))
        foreign=self.ad(model='rx')
        self.index.normalize(foreign)
        self.assertEqual(foreign['model'],'rx')
        self.assertFalse(matches(foreign,snapshot(self.filters)))


class GenerationPickerTests(SimpleTestCase):
    def rows(self):
        return [SimpleNamespace(pk=i,parent_id=100,key=f'model/g{i}',name=f'Generation {i}',kind='generation',data={})
                for i in range(1,16)]

    def test_picker_has_checkboxes_confirmation_and_keeps_page_selection(self):
        from bot.handlers.vehicle_search import picker
        kb=picker(self.rows(),100,1,['model/g1','model/g13'],True)
        self.assertTrue(kb.inline_keyboard[0][0].text.startswith('✅'))
        self.assertEqual(kb.inline_keyboard[0][0].callback_data,'car:toggle:13')
        self.assertIn('Готово · выбрано 2',[b.text for row in kb.inline_keyboard for b in row])

    async def test_toggle_changes_draft_without_search_or_save(self):
        from bot.handlers.vehicle_search import catalog_callback
        state=AsyncMock();state.get_data.return_value={'catalog_parent':100,'catalog_message_id':7,
            'catalog_selected':['model/g1'],'catalog_page':1,'catalog_query':'g'}
        callback=SimpleNamespace(data='car:toggle:13',answer=AsyncMock(),from_user=SimpleNamespace(id=123),
                                 message=SimpleNamespace(message_id=7,answer=AsyncMock(),chat=SimpleNamespace(id=123)))
        with patch('bot.handlers.vehicle_search.CatalogNode.objects.aget',AsyncMock(return_value=self.rows()[12])), \
             patch('bot.handlers.vehicle_search.show_picker',AsyncMock()) as show, \
             patch('bot.handlers.vehicle_search.save_generations',AsyncMock()) as save:
            await catalog_callback(callback,state)
        state.update_data.assert_awaited_once_with(catalog_selected=['model/g1','model/g13'])
        save.assert_not_awaited();state.clear.assert_not_awaited()
        self.assertTrue(show.await_args.kwargs['edit'])
        self.assertEqual(show.await_args.kwargs['query'],'g')

    async def test_stale_keyboard_cannot_change_selection(self):
        from bot.handlers.vehicle_search import catalog_callback
        state=AsyncMock();state.get_data.return_value={'catalog_parent':100,'catalog_message_id':8}
        callback=SimpleNamespace(data='car:toggle:1',answer=AsyncMock(),from_user=SimpleNamespace(id=123),
                                 message=SimpleNamespace(message_id=7,answer=AsyncMock(),chat=SimpleNamespace(id=123)))
        with patch('bot.handlers.vehicle_search.CatalogNode.objects.aget',AsyncMock(return_value=self.rows()[0])), \
             patch('bot.handlers.vehicle_search.save_generations',AsyncMock()) as save:
            await catalog_callback(callback,state)
        state.update_data.assert_not_awaited();save.assert_not_awaited()
class GenerationPickerFlowTests(TestCase):
    def test_open_toggle_filter_and_confirm_with_real_fsm(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.fsm.storage.base import StorageKey
        from bot.handlers.vehicle_search import show_picker, catalog_callback
        make=CatalogNode.objects.create(kind='make',key='bmw',slug='bmw',name='BMW')
        model=CatalogNode.objects.create(kind='model',key='bmw/x5',slug='x5',name='X5',parent=make)
        gens=[CatalogNode.objects.create(kind='generation',key=f'bmw/x5/g{i}',slug=f'g{i}',name=f'{i} поколение',
              parent=model,data={'year_from':2000+i}) for i in (1,2)]
        UserFilters.objects.create(user_id=123,vehicle=model)
        async def flow():
            state=FSMContext(storage=MemoryStorage(),key=StorageKey(bot_id=1,chat_id=123,user_id=123))
            message=SimpleNamespace(message_id=7,chat=SimpleNamespace(id=123),from_user=SimpleNamespace(id=0),
                                    answer=AsyncMock(),edit_text=AsyncMock())
            message.answer.return_value=message;message.edit_text.return_value=message
            with patch('bot.handlers.vehicle_search.load_children',return_value=gens):
                await show_picker(message,state,parent=model.pk,user_id=123)
                for gen in gens:
                    cb=SimpleNamespace(answer=AsyncMock(),data=f'car:toggle:{gen.pk}',message=message,from_user=SimpleNamespace(id=123))
                    await catalog_callback(cb,state)
                await show_picker(message,state,parent=model.pk,query='1',user_id=123)
                self.assertEqual(len((await state.get_data())['catalog_selected']),2)
                cb.data=f'car:apply:{model.pk}'
                await catalog_callback(cb,state)
                self.assertIsNone(await state.get_state())
            self.assertEqual(message.edit_text.await_count,2)
        async_to_sync(flow)()
        filters=UserFilters.objects.get(user_id=123)
        self.assertEqual(set(filters.generation_keys),{gen.key for gen in gens})