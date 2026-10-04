import asyncio
import logging
from html import escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from aiogram.types import BufferedInputFile
from bot.photo_delivery import download_photo
from asgiref.sync import sync_to_async

from ads.catalog import load_children, vehicle_path
from ads.catalog_search import ranked_rows
from ads.models import CatalogNode, SearchJob
from ads.search import request_search, results, snapshot, SORT_LABELS, result_order, result_page
from ads.search_status import current_status_text, selected_sources, ready_for_batch, SOURCE_NAMES
from ads.filter_values import bounds, range_label, describe, parse_price_amount
from users.models import UserFilters, User

router = Router()
PAGE_SIZE = 12

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
                ~F.text.in_({'Фильтры','Включить уведомления','Выключить уведомления','Смотреть все объявления','Очистить выбор','Включить мониторинг','Выключить мониторинг'}))
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


def picker(rows, parent, page):
    page = max(0, min(page, max(0, (len(rows)-1)//PAGE_SIZE)))
    chosen = rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
    buttons = [[Button(text=row.name[:90], callback_data=f'car:pick:{row.pk}')] for row in chosen]
    nav = []
    if page:
        nav.append(Button(text='←', callback_data=f'car:list:{parent or 0}:{page-1}'))
    if (page+1)*PAGE_SIZE < len(rows):
        nav.append(Button(text='→', callback_data=f'car:list:{parent or 0}:{page+1}'))
    if nav:
        buttons.append(nav)
    if parent:
        buttons.append([Button(text='Оставить все модели / поколения', callback_data=f'car:done:{parent}')])
    buttons.append([Button(text='Все марки', callback_data='car:all:0:0'),
                    Button(text='Популярные марки', callback_data='car:popular:0:0')])
    buttons.append([Button(text='Закрыть выбор', callback_data='car:close:0')])
    return Keyboard(inline_keyboard=buttons)


async def show_picker(message, state, parent=0, page=0, query='', mode='popular'):
    try:
        rows = await sync_to_async(load_children)(parent or None)
        parent_node = await CatalogNode.objects.aget(pk=parent) if parent else None
        rows = ranked_rows(rows, query, popular_only=not parent and mode == 'popular' and not query,
                           make_slug=parent_node.slug if parent_node and parent_node.kind == 'make' else '')
        await state.set_state(CatalogSelection.choosing)
        await state.update_data(catalog_parent=parent, catalog_query=query, catalog_mode=mode)
        await message.answer(
            ('Ничего не найдено. Попробуйте другое написание.\n' if not rows else
             f'Найдено вариантов: {len(rows)}.\n' if query else
             'Популярные марки.\n' if not parent and mode == 'popular' else 'Выберите вариант.\n') +
            'Марка → модель → поколение.\n'
            'Напишите название или его часть сообщением: BMW, бмв, ауд.\n'
            'Поиск учитывает опечатки; на следующих шагах ищет модели и поколения.',
            reply_markup=picker(rows, parent, page))
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
                ~F.text.in_({'Фильтры', 'Включить уведомления', 'Выключить уведомления', 'Смотреть все объявления','Очистить выбор','Включить мониторинг','Выключить мониторинг'}))
async def catalog_text(message, state: FSMContext):
    data = await state.get_data()
    await show_picker(message, state, parent=data.get('catalog_parent', 0),
                      query=message.text.strip()[:100], mode=data.get('catalog_mode', 'popular'))


@sync_to_async
def select_vehicle(user_id, node_id):
    User.objects.filter(user_id=user_id).update(monitoring_enabled=False)
    node = CatalogNode.objects.get(pk=node_id)
    filters, _ = UserFilters.objects.get_or_create(user_id=user_id)
    previous_model = vehicle_path(filters.vehicle).get('model') if filters.vehicle_id else None
    filters.vehicle = node
    if node.kind == 'make' or node.kind == 'model' and (not previous_model or previous_model.pk != node.pk):
        filters.variant=''
    filters.brand = [vehicle_path(node)['make'].name.lower()]
    filters.search_job = None
    filters.save()
    return node.kind


@router.callback_query(F.data.startswith('car:'))
async def catalog_callback(callback, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    _, action, node_id, *rest = callback.data.split(':')
    node_id = int(node_id)
    if action == 'close':
        await state.clear()
        await callback.message.answer('Выбор закрыт. /filters — фильтры, /search — поиск.')
        return
    if action in ('all', 'popular'):
        await show_picker(callback.message, state, mode=action)
        return
    if action == 'list':
        same_parent = data.get('catalog_parent') == node_id
        await show_picker(callback.message, state, node_id, int(rest[0]),
                          query=data.get('catalog_query', '') if same_parent else '',
                          mode=data.get('catalog_mode', 'popular'))
        return
    kind = await select_vehicle(callback.from_user.id, node_id)
    if action == 'pick' and kind != 'generation':
        await show_picker(callback.message, state, node_id)
        return
    await state.clear()
    await callback.message.answer(escape(await user_description(callback.from_user.id)) + '\n\n'
        'Другие условия: /filters. Поиск: /search. Уведомления: /notifications_on.',
        reply_markup=Keyboard(inline_keyboard=[[Button(text='Найти объявления', callback_data='search:run')]]))


@sync_to_async
def start_search(user_id):
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


async def send_card(message, ad):
    from aiogram.exceptions import TelegramBadRequest
    caption = (f'<b>{escape(ad.name[:200])}</b>\n'
               f'💰 <b>{ad.price:,} ₽</b> · {escape(str(ad.year or "—"))}\n'
               f'📍 {escape(ad.city[:100])} · {escape(ad.platform)}\n')
    if ad.mileage is not None: caption += f'Пробег: {ad.mileage:,} км\n'
    if ad.generation: caption += escape(ad.generation[:200]) + '\n'
    caption += f'<a href="{escape(ad.link,quote=True)}">Открыть объявление</a>'
    if ad.image and ad.image.startswith(('https://','http://')):
        try:
            photo = await asyncio.to_thread(download_photo,ad.image,ad.link)
        except Exception as error:
            logging.getLogger(__name__).warning('Photo download failed source=%s type=%s',ad.platform,type(error).__name__)
        else:
            try:
                await message.answer_photo(BufferedInputFile(photo,filename='car.jpg'),caption=caption,parse_mode='HTML')
                return
            except TelegramBadRequest:
                logging.getLogger(__name__).warning('Telegram rejected uploaded photo source=%s',ad.platform)
    caption += '\n📷 Фото сейчас недоступно. Посмотреть его можно в объявлении.'
    await message.answer(caption,parse_mode='HTML',disable_web_page_preview=True)


async def show_results(message, user_id, page=0, state=None):
    ads = await current_results(user_id)
    filters, _ = await UserFilters.objects.aget_or_create(user_id=user_id)
    counts = {source:sum(getattr(ad,'platform',None) == source for ad in ads) for source in selected_sources({'platform':filters.platform})}
    total = len(ads)
    ads, first_size = result_order(ads, list(counts), filters.sort_order)
    # Explicit refresh/sort rebuilds the snapshot; subsequent pages stay stable.
    if state:
        data = await state.get_data()
        if page == 0 or not data.get('result_ids'):
            await state.update_data(result_ids=[ad.pk for ad in ads], result_first_size=first_size)
        else:
            by_id = {ad.pk:ad for ad in ads}
            ads = [by_id[pk] for pk in data['result_ids'] if pk in by_id]
            first_size = data.get('result_first_size', 3)
    page = max(0,page)
    batch = result_page(ads, first_size, page)
    for ad in batch:
        await send_card(message,ad)
    buttons=[]
    consumed = first_size + page * 3
    if consumed < len(ads):
        buttons.append([Button(text='Ещё 3 машины →',callback_data=f'search:page:{page+1}')])
    buttons.append([Button(text='Обновить результаты',callback_data='search:page:0'),
                    Button(text='Статус сбора',callback_data='search:status')])
    buttons.append([Button(text=('✓ ' if filters.sort_order == key else '') + label,
                           callback_data=f'search:sort:{key}') for key,label in [('newest','Новые'),('cheap','Дешевле'),('expensive','Дороже')]])
    job = await SearchJob.objects.filter(pk=filters.search_job_id).afirst() if filters.search_job_id else None
    from ads.search_status import coverage_text
    coverage = coverage_text(counts,job.source_state if job else {})
    await message.answer(escape(await user_description(user_id)) +
        f'\n\nВ общей базе по вашим фильтрам: <b>{total}</b>. {SORT_LABELS.get(filters.sort_order,SORT_LABELS["newest"])}.\n{coverage}\n' +
        ('Первая подборка — по одной машине с каждой доступной площадки.\n' if page == 0 and filters.sort_order == 'newest' and batch else '') +
        'Сбор может продолжаться — обновляйте результаты.',
        reply_markup=Keyboard(inline_keyboard=buttons))


async def launch_search(message, user_id, state):
    from bot.interactions import active_searches
    if user_id in active_searches:
        await message.answer('Поиск уже выполняется. Дождитесь первой тройки объявлений.')
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
        '\n\n🔎 Ищу объявления на площадках. Покажу до 3 машин: по одной с Avito, Auto.ru и Дрома.')
    # Keep handlers asynchronous: other users and callbacks continue working.
    for tick in range(60):
        current = await UserFilters.objects.filter(user_id=user_id).afirst()
        if not current or current.search_job_id != job_id: return
        latest = await SearchJob.objects.aget(pk=job_id)
        if latest.requested_at != job.requested_at: return
        ads = await current_results(user_id)
        sources = selected_sources(job.filters)
        represented = {ad.platform for ad in ads}
        responded = all(ready_for_batch(latest.source_state.get(s,{}),job.requested_at.isoformat(),s in represented) for s in sources)
        if ads and (responded or tick >= 30):
            await progress.edit_text('Найдены подходящие объявления. Показываю по одной машине с доступных площадок; новые добавляются по мере сбора.')
            if not responded:
                await message.answer('Часть площадок ещё отвечает. Пока показываю доступные результаты общей базы.')
            await show_results(message,user_id,state=state)
            return
        if all(latest.source_state.get(s,{}).get('requested_at') == job.requested_at.isoformat() for s in sources):
            break
        await asyncio.sleep(2)
    await progress.edit_text('Пока подходящих объявлений нет. Проверьте состояние площадок: /status. '
                             'Если сбор продолжается, результаты доступны через /results.')
    await show_results(message,user_id,state=state)


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
