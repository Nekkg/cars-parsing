import asyncio
from weakref import WeakValueDictionary
from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from asgiref.sync import sync_to_async

from ads.catalog import load_children, vehicle_path
from ads.catalog_search import ranked_rows
from ads.models import CatalogNode, SearchJob, ResultView
from ads.search import request_search, results, snapshot, SORT_LABELS
from ads.search_status import current_status_text, selected_sources
from ads.filter_values import bounds, range_label, describe, parse_price_amount
from users.models import UserFilters, User

router = Router()
PAGE_SIZE = 12
_selection_locks = WeakValueDictionary()

class CatalogSelection(StatesGroup):
    choosing = State()

class PriceSelection(StatesGroup):
    entering = State()


@sync_to_async
def user_description(user_id):
    filters, _ = UserFilters.objects.get_or_create(user_id=user_id)
    return describe(snapshot(filters))


async def price_menu(message, user_id):
    filters, _ = await UserFilters.objects.aget_or_create(user_id=user_id)
    await message.answer('💰 Цена: ' + range_label(filters.price) + ' ₽\nВыберите «От» или «До», затем введите сумму. Можно задать одну или обе границы.',
        reply_markup=Keyboard(inline_keyboard=[
            [Button(text='От',callback_data='budget:min'),Button(text='До',callback_data='budget:max')],
            [Button(text='Без ограничений',callback_data='budget:clear')],
            [Button(text='К фильтрам',callback_data='filters_edit'),Button(text='Найти',callback_data='search:run')]]))


@router.callback_query(F.data == 'filter_Цена')
async def price_filter(callback, state: FSMContext):
    await callback.answer()
    await state.clear()
    await price_menu(callback.message,callback.from_user.id)


@router.callback_query(F.data.startswith('budget:'))
async def price_action(callback, state: FSMContext):
    await callback.answer()
    side = callback.data.split(':')[1]
    if side == 'clear':
        await UserFilters.objects.filter(user_id=callback.from_user.id).aupdate(price='',search_job=None)
        if await User.objects.filter(user_id=callback.from_user.id,monitoring_enabled=True).aexists():
            filters = await UserFilters.objects.aget(user_id=callback.from_user.id)
            await sync_to_async(request_search)(filters)
        await state.clear()
        await price_menu(callback.message,callback.from_user.id)
        return
    await state.set_state(PriceSelection.entering)
    await state.update_data(price_side=side)
    await callback.message.answer('Введите цену ' + ('от' if side == 'min' else 'до') +
                                 ' в рублях: 500000, 500 000 или 500.000. Для удаления этой границы — «нет».')


@router.message(PriceSelection.entering, F.text, ~F.text.startswith('/'),
                ~F.text.in_({'Найти машину','Отслеживание','Фильтры','Включить уведомления','Выключить уведомления','Смотреть все объявления','Очистить выбор','Включить мониторинг','Выключить мониторинг'}))
async def price_input(message, state: FSMContext):
    data = await state.get_data()
    value = message.text.strip()
    if value.casefold() in ('нет','сброс','-'):
        amount = None
    else:
        try:
            amount = parse_price_amount(value)
        except ValueError as error:
            await message.answer(str(error))
            return
    filters, _ = await UserFilters.objects.aget_or_create(user_id=message.from_user.id)
    lo, hi = bounds(filters.price)
    if data['price_side'] == 'min': lo = amount
    else: hi = amount
    value = f'{lo if lo is not None else ""}-{hi if hi is not None else ""}'
    try: bounds(value)
    except ValueError as error:
        await message.answer(str(error))
        return
    filters.price = value if lo is not None or hi is not None else ''
    filters.search_job = None
    await filters.asave(update_fields=['price','search_job'])
    if await User.objects.filter(user_id=message.from_user.id,monitoring_enabled=True).aexists():
        await sync_to_async(request_search)(filters)
    await state.clear()
    await price_menu(message,message.from_user.id)


def picker(rows, parent, page, selected=(), generations=False):
    page = max(0, min(page, max(0, (len(rows)-1)//PAGE_SIZE)))
    chosen = rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
    buttons = [[Button(text=(('✅ ' if row.key in selected else '▫️ ') if generations else '') + row.name[:86],
                       callback_data=f'car:{"toggle" if generations else "pick"}:{row.pk}')]
               for row in chosen]
    nav = []
    if page:
        nav.append(Button(text='←', callback_data=f'car:list:{parent or 0}:{page-1}'))
    if (page+1)*PAGE_SIZE < len(rows):
        nav.append(Button(text='→', callback_data=f'car:list:{parent or 0}:{page+1}'))
    if nav:
        buttons.append(nav)
    if generations:
        buttons.append([Button(text=f'Готово · выбрано {len(selected)}', callback_data=f'car:apply:{parent}')])
        buttons.append([Button(text='Любое поколение', callback_data=f'car:done:{parent}')])
    elif parent:
        buttons.append([Button(text='Оставить все модели', callback_data=f'car:done:{parent}')])
    buttons.append([Button(text='Все марки', callback_data='car:all:0:0'),
                    Button(text='Популярные марки', callback_data='car:popular:0:0')])
    buttons.append([Button(text='Закрыть выбор', callback_data='car:close:0')])
    return Keyboard(inline_keyboard=buttons)


@sync_to_async
def saved_generations(user_id, model_id):
    filters = UserFilters.objects.filter(user_id=user_id).select_related('vehicle').first()
    if not filters:
        return []
    path = vehicle_path(filters.vehicle)
    model = path.get('model')
    target = CatalogNode.objects.get(pk=model_id)
    if not model or target.key not in (model.key, model.data.get('canonical_model_key')):
        return []
    return filters.generation_keys or ([path['generation'].key] if path.get('generation') else [])


async def show_picker(message, state, parent=0, page=0, query='', mode='popular', user_id=None, edit=False):
    try:
        rows = await sync_to_async(load_children)(parent or None)
        parent_node = await CatalogNode.objects.aget(pk=parent) if parent else None
        generations = bool(parent_node and parent_node.kind == 'model')
        if generations and rows:
            # Catalogue aliases can redirect to a canonical model's generations.
            parent = rows[0].parent_id
        data = await state.get_data()
        selected = data.get('catalog_selected', []) if data.get('catalog_parent') == parent else None
        if selected is None:
            selected = await saved_generations(user_id or message.from_user.id, parent) if generations else []
        rows = ranked_rows(rows, query, popular_only=not parent and mode == 'popular' and not query,
                           make_slug=parent_node.slug if parent_node and parent_node.kind == 'make' else '')
        await state.set_state(CatalogSelection.choosing)
        await state.update_data(catalog_parent=parent, catalog_query=query, catalog_mode=mode,
                                catalog_page=page, catalog_selected=selected)
        prompt = ('Ничего не найдено. Попробуйте другое написание.\n' if not rows else
                  f'Найдено вариантов: {len(rows)}.\n' if query else
                  'Популярные марки.\n' if not parent and mode == 'popular' else 'Выберите вариант.\n')
        prompt += ('Отметьте одно или несколько поколений и нажмите «Готово».\n'
                   'Повторное нажатие снимает галочку. Пустой выбор — любое поколение.\n'
                   'Напишите название, код кузова или его часть для поиска.' if generations else
                   'Марка → модель → поколение.\n'
                   'Напишите название или его часть сообщением: BMW, бмв, ауд.\n'
                   'Поиск учитывает опечатки; на следующих шагах ищет модели и поколения.')
        keyboard = picker(rows, parent, page, selected, generations)
        if edit:
            sent = await message.edit_text(prompt, reply_markup=keyboard)
        else:
            sent = await message.answer(prompt, reply_markup=keyboard)
        await state.update_data(catalog_message_id=sent.message_id)
    except Exception:
        await message.answer('Не удалось загрузить этот раздел каталога. Выбор сохранён; повторите /car позже.')


@router.message(Command('car'))
async def choose_car(message, state: FSMContext):
    await state.clear()
    query = (message.text.split(maxsplit=1) + [''])[1]
    await show_picker(message, state, query=query)


@router.callback_query(F.data == 'filter_Марка')
async def brand_filter(callback, state: FSMContext):
    await callback.answer()
    await state.clear()
    await show_picker(callback.message, state)


@router.message(CatalogSelection.choosing, F.text, ~F.text.startswith('/'),
                ~F.text.in_({'Найти машину','Отслеживание','Фильтры', 'Включить уведомления', 'Выключить уведомления', 'Смотреть все объявления','Очистить выбор','Включить мониторинг','Выключить мониторинг'}))
async def catalog_text(message, state: FSMContext):
    data = await state.get_data()
    await show_picker(message, state, parent=data.get('catalog_parent', 0),
                      query=message.text.strip()[:100], mode=data.get('catalog_mode', 'popular'))


@sync_to_async
def select_vehicle(user_id, node_id):
    ResultView.objects.filter(user_id=user_id,live_until__isnull=False).update(live_until=None,collect_until=None)
    User.objects.filter(user_id=user_id).update(monitoring_enabled=False)
    node = CatalogNode.objects.get(pk=node_id)
    filters, _ = UserFilters.objects.get_or_create(user_id=user_id)
    previous_model = vehicle_path(filters.vehicle).get('model') if filters.vehicle_id else None
    previous_generation = vehicle_path(filters.vehicle).get('generation') if filters.vehicle_id else None
    filters.vehicle = node
    filters.generation_hint = ''
    current_model = vehicle_path(node).get('model')
    if node.kind == 'generation' or not current_model or not previous_model or current_model.pk != previous_model.pk:
        filters.generation_keys = []
    elif previous_generation and not filters.generation_keys:
        filters.generation_keys = [previous_generation.key]
    if node.kind == 'make' or node.kind == 'model' and (not previous_model or previous_model.pk != node.pk):
        filters.variant=''
    filters.brand = [vehicle_path(node)['make'].name.lower()]
    filters.search_job = None
    filters.save()
    return node.kind


@sync_to_async
def save_generations(user_id, model_id, keys):
    from django.db import transaction
    with transaction.atomic():
        model = CatalogNode.objects.get(pk=model_id, kind='model')
        valid = set(CatalogNode.objects.filter(kind='generation', parent=model, key__in=keys).values_list('key', flat=True))
        if set(keys) != valid:
            raise ValueError('Generation does not belong to the selected model')
        filters, _ = UserFilters.objects.select_for_update().get_or_create(user_id=user_id)
        current = vehicle_path(filters.vehicle).get('model')
        if not current or model.key not in (current.key, current.data.get('canonical_model_key')):
            raise ValueError('Selection changed')
        filters.vehicle = model
        filters.generation_keys = sorted(valid)
        filters.generation_hint = ''
        filters.search_job = None
        filters.save(update_fields=['vehicle', 'generation_keys', 'generation_hint', 'search_job'])
        ResultView.objects.filter(user_id=user_id, live_until__isnull=False).update(live_until=None, collect_until=None)
        User.objects.filter(user_id=user_id).update(monitoring_enabled=False)


@router.callback_query(F.data.startswith('car:'))
async def catalog_callback(callback, state: FSMContext):
    await callback.answer()
    key = (callback.from_user.id, callback.message.chat.id)
    lock = _selection_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _selection_locks[key] = lock
    async with lock:
        await apply_catalog_callback(callback, state)


async def apply_catalog_callback(callback, state: FSMContext):
    data = await state.get_data()
    _, action, node_id, *rest = callback.data.split(':')
    node_id = int(node_id)
    if action == 'close':
        await state.clear()
        await callback.message.answer('Выбор закрыт. /filters — фильтры, /search — поиск.')
        return
    if action in ('all', 'popular'):
        await show_picker(callback.message, state, mode=action, user_id=callback.from_user.id)
        return
    if action == 'list':
        same_parent = data.get('catalog_parent') == node_id
        await show_picker(callback.message, state, node_id, int(rest[0]),
                          query=data.get('catalog_query', '') if same_parent else '',
                          mode=data.get('catalog_mode', 'popular'), user_id=callback.from_user.id)
        return
    node = await CatalogNode.objects.aget(pk=node_id)
    if action in ('toggle', 'apply') or action == 'done' and node.kind == 'model':
        parent_id = node.parent_id if action == 'toggle' else node.pk
        if data.get('catalog_parent') != parent_id or data.get('catalog_message_id') != callback.message.message_id:
            await callback.message.answer('Этот выбор устарел. Откройте выбор поколения заново.')
            return
        selected = list(data.get('catalog_selected', []))
        if action == 'toggle':
            if node.kind != 'generation':
                return
            if node.key in selected:
                selected.remove(node.key)
            else:
                selected.append(node.key)
            await state.update_data(catalog_selected=selected)
            await show_picker(callback.message, state, parent_id, data.get('catalog_page', 0),
                              query=data.get('catalog_query', ''), user_id=callback.from_user.id, edit=True)
            return
        try:
            await save_generations(callback.from_user.id, node.pk, selected if action == 'apply' else [])
        except ValueError:
            await callback.message.answer('Выбор изменился. Откройте поколения заново.')
            return
    else:
        kind = await select_vehicle(callback.from_user.id, node_id)
        if action == 'pick' and kind != 'generation':
            await show_picker(callback.message, state, node_id, user_id=callback.from_user.id)
            return
    await state.clear()
    await callback.message.answer(escape(await user_description(callback.from_user.id)) + '\n\n'
        'Другие условия: /filters. Поиск: /search.', parse_mode='HTML',
        reply_markup=Keyboard(inline_keyboard=[[Button(text='Найти объявления', callback_data='search:run')]]))


@sync_to_async
def start_search(user_id):
    ResultView.objects.filter(user_id=user_id,live_until__isnull=False).update(live_until=None,collect_until=None)
    User.objects.filter(user_id=user_id).update(monitoring_enabled=False)
    filters, _ = UserFilters.objects.get_or_create(user_id=user_id)
    if filters.sort_order != 'newest':
        filters.sort_order = 'newest'
        filters.save(update_fields=['sort_order'])
    User.objects.get_or_create(user_id=user_id)
    return request_search(filters).pk


@sync_to_async
def current_results(user_id):
    filters, _ = UserFilters.objects.get_or_create(user_id=user_id)
    return results(snapshot(filters),filters.sort_order)


async def show_results(message,user_id,page=0,state=None):
    from bot.handlers.result_browser import open_browser
    return await open_browser(message,user_id)

async def launch_search(message, user_id, state):
    from bot.interactions import active_searches
    if user_id in active_searches:
        await message.answer('Поиск уже выполняется. Дождитесь карточки результатов.')
        return
    active_searches.add(user_id)
    try:
        await run_search_once(message,user_id,state)
    finally:
        active_searches.discard(user_id)


async def run_search_once(message,user_id,state):
    await state.clear()
    job_id = await start_search(user_id)
    job = await SearchJob.objects.aget(pk=job_id)
    progress = await message.answer(escape(await user_description(user_id)) +
        '\n\n🔎 Ищу объявления на Avito, Auto.ru и Дроме. Карточку можно листать стрелками и переключать площадки.')
    # Keep handlers asynchronous: other users and callbacks continue working.
    for tick in range(60):
        current = await UserFilters.objects.filter(user_id=user_id).afirst()
        if not current or current.search_job_id != job_id: return
        latest = await SearchJob.objects.aget(pk=job_id)
        if latest.requested_at != job.requested_at: return
        ads = await current_results(user_id)
        sources = selected_sources(job.filters)
        if ads:
            from bot.handlers.result_browser import open_browser
            await open_browser(message,user_id,progress)
            return
        if all(latest.source_state.get(s,{}).get('requested_at') == job.requested_at.isoformat() for s in sources):
            break
        await asyncio.sleep(2)
    from bot.handlers.result_browser import open_browser
    await open_browser(message,user_id,progress)

@router.message(Command('search'))
async def search_command(message, state: FSMContext):
    await launch_search(message,message.from_user.id,state)


@router.message(Command('results'))
async def results_command(message, state: FSMContext):
    await show_results(message,message.from_user.id,state=state)


@router.callback_query(F.data.startswith('search:'))
async def search_callback(callback, state: FSMContext):
    await callback.answer()
    from bot.interactions import duplicate_click
    if duplicate_click(callback.from_user.id,callback.data): return
    if callback.data == 'search:run':
        await launch_search(callback.message,callback.from_user.id,state)
        return
    if callback.data == 'search:status':
        await show_status(callback.message,callback.from_user.id)
        return
    if callback.data.startswith('search:sort:'):
        order = callback.data.rsplit(':',1)[1]
        if order in SORT_LABELS:
            await UserFilters.objects.filter(user_id=callback.from_user.id).aupdate(sort_order=order)
            await show_results(callback.message,callback.from_user.id,state=state)
        return
    await show_results(callback.message,callback.from_user.id,
                       int(callback.data.rsplit(':',1)[1]) if ':page:' in callback.data else 0,state)

@router.message(Command('status'))
async def status_command(message):
    await show_status(message,message.from_user.id)


async def show_status(message,user_id):
    filters = await UserFilters.objects.filter(user_id=user_id).afirst()
    if not filters or not filters.search_job_id:
        await message.answer('Сначала выберите автомобиль через /car и запустите /search.')
        return
    job = await SearchJob.objects.aget(pk=filters.search_job_id)
    await message.answer(escape(await sync_to_async(current_status_text)(job)))
