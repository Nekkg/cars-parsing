from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from django.test import SimpleTestCase
from ads.catalog_search import ranked_rows


def node(pk, name, slug, kind='make'):
    return SimpleNamespace(pk=pk, name=name, slug=slug, kind=kind)


class CatalogSearchTests(SimpleTestCase):
    def setUp(self):
        self.rows = [node(1,'Acura','acura'), node(2,'Audi','audi'),
                     node(3,'BMW','bmw'), node(4,'Toyota','toyota'),
                     node(5,'Mercedes-Benz','mercedes-benz')]

    def test_popular_first_and_rare_still_accessible(self):
        self.assertEqual([r.name for r in ranked_rows(self.rows, popular_only=True)],
                         ['BMW','Audi','Mercedes-Benz','Toyota'])
        self.assertEqual(ranked_rows(self.rows)[-1].name,'Acura')
        self.assertEqual(ranked_rows(self.rows,'acur',popular_only=True)[0].name,'Acura')

    def test_alias_partial_typo_and_case(self):
        for query, expected in [('бмв','BMW'), (' BmW ','BMW'), ('ауд','Audi'),
                                ('мерс','Mercedes-Benz'), ('toytoa','Toyota')]:
            with self.subTest(query=query):
                self.assertEqual(ranked_rows(self.rows,query)[0].name,expected)

    def test_unknown_query_and_exact_model_priority(self):
        self.assertEqual(ranked_rows(self.rows,'абракадабра'),[])
        rows = [node(1,'X5 M','x5_m','model'),node(2,'X5','x5','model')]
        self.assertEqual([r.name for r in ranked_rows(rows,'x5')],['X5','X5 M'])

    async def test_text_search_preserves_parent_and_pagination_query(self):
        from bot.handlers.vehicle_search import catalog_text, catalog_callback
        state = AsyncMock()
        state.get_data.return_value = {'catalog_parent':12,'catalog_query':'x', 'catalog_mode':'all'}
        message = SimpleNamespace(text='x5',chat=SimpleNamespace(id=9))
        callback = SimpleNamespace(data='car:list:12:1',answer=AsyncMock(),message=message,from_user=SimpleNamespace(id=9))
        with patch('bot.handlers.vehicle_search.show_picker',new_callable=AsyncMock) as show:
            await catalog_text(message,state)
            show.assert_awaited_once_with(message,state,parent=12,query='x5',mode='all')
            show.reset_mock()
            await catalog_callback(callback,state)
            show.assert_awaited_once_with(message,state,12,1,query='x',mode='all',user_id=9)

    async def test_full_catalog_clears_query_and_close_exits_state(self):
        from bot.handlers.vehicle_search import catalog_callback
        state = AsyncMock()
        state.get_data.return_value = {'catalog_query':'bmw'}
        callback = SimpleNamespace(data='car:all:0:0',answer=AsyncMock(),message=AsyncMock(),from_user=SimpleNamespace(id=9))
        with patch('bot.handlers.vehicle_search.show_picker',new_callable=AsyncMock) as show:
            await catalog_callback(callback,state)
            show.assert_awaited_once_with(callback.message,state,mode='all',user_id=9)
        callback.data='car:close:0'
        await catalog_callback(callback,state)
        state.clear.assert_awaited_once()
