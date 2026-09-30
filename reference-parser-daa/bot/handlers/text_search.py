from html import escape
from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from asgiref.sync import sync_to_async
from ads.catalog import load_children
from ads.models import CatalogNode
from ads.text_query import split_vehicle_query, model_suggestions
from ads.search import request_search
from users.models import UserFilters, User
from bot.keyboards.reply.start_kb import start_keyboard

router=Router()
HINT='Можно просто написать марку, модель и год: <code>bmw m340 2018</code> или <code>bmw 3</code>. Я уточню модель и предложу выбрать поколение.'
MENU_TEXTS={'Фильтры','Очистить выбор','Включить уведомления','Выключить уведомления',
            'Включить мониторинг','Выключить мониторинг','Смотреть все объявления'}

class VehicleQuery(StatesGroup):
    waiting=State()
    confirming=State()


async def open_filters(message,state,user_id=None):
    from bot.services.filters_service import get_user_filters
    from bot.keyboards.inline.filters_kb import filters_keyboard
    await state.clear()
    await User.objects.filter(user_id=user_id or message.from_user.id).aupdate(monitoring_enabled=False)
    await state.set_state(VehicleQuery.waiting)
    await message.answer(HINT,reply_markup=start_keyboard)
    await message.answer(await get_user_filters(user_id or message.from_user.id),reply_markup=filters_keyboard(),parse_mode='HTML')


@sync_to_async
def resolve_query(text):
    parsed=split_vehicle_query(text,load_children())
    if not parsed: return None
    make,model_text,year=parsed
    models,variant=model_suggestions(make,model_text,load_children(make.pk)) if model_text else ([], '')
    return {'make_id':make.pk,'make_name':make.name,'models':[(m.pk,m.name) for m in models],
            'year':year,'variant':variant,'model_text':model_text}


@sync_to_async
def save_choice(user_id,node_id,year,variant):
    from ads.catalog import vehicle_path
    User.objects.filter(user_id=user_id).update(monitoring_enabled=False)
    node=CatalogNode.objects.get(pk=node_id)
    filters,_=UserFilters.objects.get_or_create(user_id=user_id)
    filters.vehicle=node
    filters.brand=[vehicle_path(node)['make'].name.lower()]
    filters.year=f'{year}-{year}' if year else ''
    filters.variant=variant
    filters.search_job=None
    filters.save()


async def confirm_model(message,state,user_id,node_id,data):
    from bot.handlers.vehicle_search import user_description
    await save_choice(user_id,node_id,data['year'],data['variant'])
    await state.set_state(VehicleQuery.confirming)
    await state.update_data(query_model=node_id)
    await message.answer('Распознал запрос:\n'+escape(await user_description(user_id))+
        '\n\nВыбрать поколение? Можно искать без ограничения по поколению.',
        reply_markup=Keyboard(inline_keyboard=[
            [Button(text='Выбрать поколение',callback_data=f'query:generation:{node_id}')],
            [Button(text='Искать без поколения',callback_data='query:search')],
            [Button(text='Очистить выбор',callback_data='selection:clear')]]))


@router.message(StateFilter(VehicleQuery.waiting,VehicleQuery.confirming,None),F.text,~F.text.startswith('/'),~F.text.in_(MENU_TEXTS))
async def query_message(message,state:FSMContext):
    try: data=await resolve_query(message.text[:200])
    except ValueError as error:
        await message.answer(str(error));return
    except Exception:
        await message.answer('Не удалось загрузить каталог. Попробуйте чуть позже.');return
    if not data:
        await message.answer('Не распознал марку. '+HINT);return
    await state.update_data(vehicle_query=data)
    if not data['model_text']:
        from bot.handlers.vehicle_search import show_picker
        await save_choice(message.from_user.id,data['make_id'],data['year'],'')
        await show_picker(message,state,parent=data['make_id']);return
    if not data['models']:
        await message.answer('Не нашёл такую модель у '+escape(data['make_name'])+'. Уточните название.');return
    if len(data['models'])==1:
        await confirm_model(message,state,message.from_user.id,data['models'][0][0],data);return
    await state.set_state(VehicleQuery.confirming)
    await message.answer('Уточните модель:',reply_markup=Keyboard(inline_keyboard=[
        [Button(text=f'{data["make_name"]} {name}',callback_data=f'query:model:{pk}')] for pk,name in data['models']]))


@router.callback_query(F.data.startswith('query:'))
async def query_callback(callback,state:FSMContext):
    await callback.answer()
    data=await state.get_data()
    action=callback.data.split(':')[1]
    if action=='model':
        pk=int(callback.data.rsplit(':',1)[1]);query=data.get('vehicle_query')
        if not query or pk not in [row[0] for row in query['models']]:
            await callback.message.answer('Этот выбор устарел. Напишите запрос заново.');return
        await confirm_model(callback.message,state,callback.from_user.id,pk,query)
    elif action=='generation':
        from bot.handlers.vehicle_search import show_picker
        pk=int(callback.data.rsplit(':',1)[1])
        if data.get('query_model')!=pk:
            await callback.message.answer('Этот выбор устарел. Напишите запрос заново.');return
        await show_picker(callback.message,state,parent=pk)
    elif action=='search':
        from bot.handlers.vehicle_search import launch_search
        await launch_search(callback.message,callback.from_user.id,state)


@sync_to_async
def clear_selection(user_id):
    filters,_=UserFilters.objects.get_or_create(user_id=user_id)
    for field in UserFilters._meta.fields:
        if field.name not in ('id','user_id','sort_order'):
            setattr(filters,field.name,field.get_default() if field.has_default() else None if field.null else '')
    filters.save()
    # An empty filter must never enable notifications for every car in the database.
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
