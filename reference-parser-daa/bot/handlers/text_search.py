from html import escape
from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from asgiref.sync import sync_to_async
from ads.catalog import load_children
from ads.catalog_search import normalize
from ads.models import CatalogNode, ResultView
from ads.text_query import model_suggestions
from ads.query_language import parse_request, model_aliases
from users.models import UserFilters, User
from bot.keyboards.reply.start_kb import start_keyboard

router = Router()
HINT = ('✍️ Быстрее всего — написать одной строкой, например:\n'
        '<code>камри 2018–2020 калининград</code>\n'
        '<code>бмв g20 москва до 4 млн</code>\n'
        '<code>мерс c200 до 2015 спб</code>\n'
        '<code>солярис 2017–2020 до 1.2 млн казань +200</code>\n\n'
        'Можно указать цену «от» и «до», пробег до 150 тыс км, дизель/бензин, '
        'автомат/механику, привод, кузов и площадку. Для неполного запроса предложу варианты.')
MENU_TEXTS = {'Найти машину','Отслеживание','Фильтры','Очистить выбор','Включить уведомления','Выключить уведомления',
              'Включить мониторинг','Выключить мониторинг','Смотреть все объявления'}

class VehicleQuery(StatesGroup):
    waiting = State()
    confirming = State()


async def open_filters(message,state,user_id=None):
    from bot.services.filters_service import get_user_filters
    from bot.keyboards.inline.filters_kb import filters_keyboard
    await state.clear()
    await ResultView.objects.filter(user_id=user_id or message.from_user.id,live_until__isnull=False).aupdate(live_until=None,collect_until=None)
    await User.objects.filter(user_id=user_id or message.from_user.id).aupdate(monitoring_enabled=False)
    await state.set_state(VehicleQuery.waiting)
    await message.answer(HINT,reply_markup=start_keyboard,parse_mode='HTML')
    await message.answer(await get_user_filters(user_id or message.from_user.id),reply_markup=filters_keyboard(),parse_mode='HTML')


@sync_to_async
def resolve_query(text):
    from ads.vehicle_query import resolve_query as resolve
    return resolve(text)


@sync_to_async
def save_choice(user_id,node_id,year,variant,extra=None):
    from ads.catalog import vehicle_path
    ResultView.objects.filter(user_id=user_id,live_until__isnull=False).update(live_until=None,collect_until=None)
    User.objects.filter(user_id=user_id).update(monitoring_enabled=False)
    node = CatalogNode.objects.get(pk=node_id)
    filters,_ = UserFilters.objects.get_or_create(user_id=user_id)
    if extra is not None:
        # A new complete sentence starts a new request, without stale constraints.
        for field in UserFilters._meta.fields:
            if field.name not in ('id','user_id','sort_order'):
                setattr(filters,field.name,field.get_default() if field.has_default() else None if field.null else '')
        for name in ('price','city','location','radius_km','engine','gearbox','drive','body_type','mileage','owners','color','steering','condition','seller','platform','generation_hint'):
            setattr(filters,name,extra.get(name,UserFilters._meta.get_field(name).get_default()))
    filters.generation_keys = []
    filters.vehicle = node
    filters.brand = [vehicle_path(node)['make'].name.lower()]
    filters.year = year if '-' in str(year) else f'{year}-{year}' if year else ''
    filters.variant = variant
    filters.search_job = None
    filters.save()


async def confirm_model(message,state,user_id,node_id,data):
    from bot.handlers.vehicle_search import user_description, launch_search
    await save_choice(user_id,node_id,data['year'],data['variant'],data.get('extra'))
    extra = data.get('extra',{})
    if any(extra.get(key) for key in ('year','price','city','generation_hint','engine','gearbox','drive','body_type','mileage','owners','color','steering','condition','seller','platform')):
        await launch_search(message,user_id,state)
        return
    await state.set_state(VehicleQuery.confirming)
    await state.update_data(query_model=node_id)
    await message.answer('Распознал запрос:\n'+escape(await user_description(user_id))+
        '\n\nВыбрать поколение? Можно искать без ограничения по поколению.',parse_mode='HTML',
        reply_markup=Keyboard(inline_keyboard=[
            [Button(text='Выбрать поколение',callback_data=f'query:generation:{node_id}')],
            [Button(text='Искать без поколения',callback_data='query:search')],
            [Button(text='Очистить выбор',callback_data='selection:clear')]]))


async def offer_query(message,state,user_id,data):
    if data.get('extra',{}).get('city_choices'):
        await state.set_state(VehicleQuery.confirming)
        await message.answer('Уточните город:',reply_markup=Keyboard(inline_keyboard=[
            [Button(text=(city['name']+' · '+city['region'])[:90],callback_data=f'query:city:{index}')]
            for index,city in enumerate(data['extra']['city_choices'])]))
        return
    if not data['model_text']:
        from bot.handlers.vehicle_search import show_picker
        await save_choice(user_id,data['make_id'],data['year'],'',data.get('extra'))
        await show_picker(message,state,parent=data['make_id'])
    elif not data['models']:
        await message.answer('Не распознал модель или часть условий у '+escape(data['make_name'])+
                             '. Уточните запрос.\n\n'+HINT,parse_mode='HTML')
    elif len(data['models'])==1 and not data.get('needs_confirmation'):
        await confirm_model(message,state,user_id,data['models'][0][0],data)
    else:
        await state.set_state(VehicleQuery.confirming)
        prompt = 'Уточните модель:' if not data.get('needs_confirmation') else 'Не распознал название целиком: '+escape(data['model_text'])+'. Выберите модель из вариантов. Если здесь есть дополнительные условия — уточните запрос.'
        await message.answer(prompt,parse_mode='HTML',reply_markup=Keyboard(inline_keyboard=[
            [Button(text=f'{data["make_name"]} {name}'[:90],callback_data=f'query:model:{pk}')] for pk,name in data['models']]))


@router.message(StateFilter(VehicleQuery.waiting,VehicleQuery.confirming,None),F.text,~F.text.startswith('/'),~F.text.in_(MENU_TEXTS))
async def query_message(message,state:FSMContext):
    try: data = await resolve_query(message.text)
    except ValueError as error:
        await message.answer(escape(str(error)),parse_mode='HTML'); return
    except Exception:
        await message.answer('Не удалось загрузить каталог. Попробуйте чуть позже.'); return
    if not data:
        await message.answer('Не распознал автомобиль. '+HINT,parse_mode='HTML'); return
    await state.update_data(vehicle_query=data,query_model=None)
    await offer_query(message,state,message.from_user.id,data)


@router.callback_query(F.data.startswith('query:'))
async def query_callback(callback,state:FSMContext):
    await callback.answer()
    data = await state.get_data()
    query = data.get('vehicle_query')
    try:
        action = callback.data.split(':')[1]
        if action=='model':
            pk = int(callback.data.rsplit(':',1)[1])
            if not query or pk not in [row[0] for row in query['models']]: raise ValueError
            await confirm_model(callback.message,state,callback.from_user.id,pk,query)
        elif action=='city':
            index = int(callback.data.rsplit(':',1)[1])
            if index<0 or not query: raise ValueError
            city = query['extra']['city_choices'][index]
            query['extra'].update(city=[city['name']],location=city,city_choices=[])
            await state.update_data(vehicle_query=query)
            await offer_query(callback.message,state,callback.from_user.id,query)
        elif action=='generation':
            from bot.handlers.vehicle_search import show_picker
            pk = int(callback.data.rsplit(':',1)[1])
            if data.get('query_model')!=pk: raise ValueError
            await show_picker(callback.message,state,parent=pk,user_id=callback.from_user.id)
        elif action=='search':
            from bot.handlers.vehicle_search import launch_search
            if not data.get('query_model'): raise ValueError
            await launch_search(callback.message,callback.from_user.id,state)
    except (ValueError,IndexError,KeyError):
        await callback.message.answer('Этот выбор устарел. Напишите запрос заново.')


@sync_to_async
def clear_selection(user_id):
    filters,_ = UserFilters.objects.get_or_create(user_id=user_id)
    for field in UserFilters._meta.fields:
        if field.name not in ('id','user_id','sort_order'):
            setattr(filters,field.name,field.get_default() if field.has_default() else None if field.null else '')
    filters.save()
    ResultView.objects.filter(user_id=user_id,live_until__isnull=False).update(live_until=None,collect_until=None)
    User.objects.filter(user_id=user_id).update(monitoring_enabled=False)


async def reset_selection(message,state,user_id):
    await clear_selection(user_id)
    await state.clear()
    await message.answer('Выбор очищен. Уведомления выключены до настройки нового поиска.',reply_markup=start_keyboard)
    await open_filters(message,state,user_id)


@router.message(F.text=='Очистить выбор')
async def reset_message(message,state:FSMContext):
    await reset_selection(message,state,message.from_user.id)


@router.callback_query(F.data=='selection:clear')
async def reset_callback(callback,state:FSMContext):
    await callback.answer()
    await reset_selection(callback.message,state,callback.from_user.id)
