"""Save a result query, choose a frequency and manage multiple tracked searches."""
from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
from asgiref.sync import sync_to_async
from django.utils import timezone
from django.core.exceptions import ValidationError

from ads.models import ResultView
from ads.filter_values import bounds, describe
from ads.search import snapshot
from ads.search_status import SOURCE_NAMES, selected_sources
from users.models import TrackedSearch, UserFilters
from bot.keyboards.reply.start_kb import start_keyboard
from bot.services import tracking_service as service

router = Router()
PAGE_SIZE = 6


def query_description(data):
    lines = describe(data).splitlines()
    if data.get('year'):
        lo, hi = bounds(data['year'])
        year = str(lo) if lo is not None and lo == hi else ' · '.join(
            f'{label} {n}' for label, n in (('от', lo), ('до', hi)) if n is not None)
        lines = ['Год: ' + year if line.startswith('Год:') else line for line in lines]
    if data.get('platform'):
        names = ', '.join(SOURCE_NAMES[p] for p in selected_sources(data))
        lines = ['Площадки: ' + names if line.startswith('Площадки:') else line for line in lines]
    return '\n'.join(lines)


def query_title(data):
    return query_description(data).replace('🚗 ', '', 1).replace('\n', ' · ')


def interval_keyboard(prefix, selected=None):
    return Keyboard(inline_keyboard=[[Button(text=('✓ ' if n == selected else '') + f'{n} мин',
        callback_data=f'{prefix}:{n}') for n in service.INTERVALS]])


def detail_keyboard(row, notification=False):
    rows = [[Button(text='📋 Объявления', callback_data=f'track:results:{row.pk}')]]
    if not notification:
        rows.extend([
            [Button(text='⏱ Частота', callback_data=f'track:frequency:{row.pk}'),
             Button(text='⏸ Пауза' if row.enabled else '▶️ Продолжить', callback_data=f'track:{"pause" if row.enabled else "resume"}:{row.pk}')],
            [Button(text='🗑 Удалить', callback_data=f'track:delete:{row.pk}')]])
    rows.append([Button(text='🔔 Все отслеживания', callback_data='track:list:0')])
    return Keyboard(inline_keyboard=rows)


def detail_text(row):
    status = '🟢 Включено' if row.enabled else '⏸ На паузе'
    platforms = selected_sources(row.filters)
    sources = ', '.join(SOURCE_NAMES.get(p, p) for p in platforms)
    text = '🔔 <b>Отслеживание</b>\n' + escape(query_description(row.filters))
    text += f'\n\n{status} · проверка каждые {row.interval_minutes} мин'
    if not row.filters.get('platform'):
        text += '\nПлощадки: ' + escape(sources)
    if row.enabled:
        seconds = max(0, (row.next_check_at - timezone.now()).total_seconds())
        text += '\nСледующая проверка: ' + (f'через {max(1, int((seconds + 59) // 60))} мин' if seconds else 'в очереди')
        if row.last_check_at:
            elapsed = max(0, int((timezone.now() - row.last_check_at).total_seconds() // 60))
            text += '\nПоследняя проверка: ' + (f'{elapsed} мин назад' if elapsed else 'только что')
        blocked = [SOURCE_NAMES.get(p, p) for p in platforms if row.search_job.source_state.get(p, {}).get('error_code') == 'blocked']
        if blocked:
            text += '\nПауза доступа: ' + escape(', '.join(blocked))
    return text


async def offer_view(message, view):
    data = service.view_query(view)
    try:
        service.validate_query(data)
    except ValueError as error:
        await message.answer(str(error), reply_markup=start_keyboard)
        return
    row = await TrackedSearch.objects.filter(user_id=view.user_id, fingerprint=service.fingerprint(data)).afirst()
    title = query_title(data)
    await message.answer('⏱ Слежу за «' + escape(title[:500]) + '».\nКак часто проверять?',
        reply_markup=interval_keyboard(f'track:add:{view.pk.hex}:{view.platform}', row.interval_minutes if row and row.enabled else None))


async def offer_current(message, user_id):
    filters, _ = await UserFilters.objects.aget_or_create(user_id=user_id)
    data = await sync_to_async(snapshot)(filters)
    try:
        service.validate_query(data)
    except ValueError as error:
        await message.answer(str(error), reply_markup=start_keyboard)
        return
    view = await ResultView.objects.acreate(user_id=user_id, chat_id=message.chat.id, filters=data)
    await offer_view(message, view)


@router.message(F.text == 'Найти машину')
async def find_car(message, state: FSMContext):
    filters = await UserFilters.objects.filter(user_id=message.from_user.id).afirst()
    if filters and (filters.vehicle_id or filters.brand or filters.variant):
        from bot.handlers.vehicle_search import launch_search
        await launch_search(message, message.from_user.id, state)
    else:
        from bot.handlers.text_search import open_filters
        await open_filters(message, state)


@router.message(F.text == 'Отслеживание')
@router.message(Command('tracking'))
async def tracking_menu(message, state: FSMContext):
    await state.clear()
    await show_list(message, message.from_user.id)


async def show_list(message, user_id, page=0, edit=False):
    qs = TrackedSearch.objects.filter(user_id=user_id, chat_id=message.chat.id).order_by('-created_at', '-id')
    total = await qs.acount()
    page = max(0, min(page, max(0, (total - 1) // PAGE_SIZE)))
    rows = [row async for row in qs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    if not rows:
        text = '🔔 Пока нет отслеживаемых машин.\nНайдите автомобиль и нажмите «Следить» в выдаче.'
        if edit:
            await message.edit_text(text, reply_markup=Keyboard(inline_keyboard=[]))
        else:
            await message.answer(text, reply_markup=start_keyboard)
        return
    text = '<b>🔔 Отслеживание</b>\nВыберите запрос, чтобы изменить частоту, поставить на паузу или удалить.\n'
    buttons = []
    for index, row in enumerate(rows, page * PAGE_SIZE + 1):
        title = query_title(row.filters)
        text += f'\n{index}. {"🟢" if row.enabled else "⏸"} {escape(title[:350])}\nПроверка: каждые {row.interval_minutes} мин\n'
        buttons.append([Button(text=f'{index}. {title[:60]}', callback_data=f'track:open:{row.pk}')])
    nav = []
    if page:
        nav.append(Button(text='⬅️', callback_data=f'track:list:{page - 1}'))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(Button(text='➡️', callback_data=f'track:list:{page + 1}'))
    if nav:
        buttons.append(nav)
    buttons.append([Button(text='🔄 Обновить список', callback_data=f'track:list:{page}')])
    markup = Keyboard(inline_keyboard=buttons)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def refresh_view(message, view):
    if not view.message_id:
        return
    from bot.handlers import result_browser as browser
    async with browser.lock_for(view.pk):
        await view.arefresh_from_db()
        try:
            await browser.render_card(message, view)
            if view.list_message_id:
                await browser.render_list(message, view)
        except (TelegramBadRequest, TelegramRetryAfter):
            pass  # A deleted result card does not cancel the saved subscription.


async def add_from_view(callback, parts):
    _, _, token, source, interval = parts
    view = await ResultView.objects.filter(pk=token, user_id=callback.from_user.id, chat_id=callback.message.chat.id,
        created_at__gte=timezone.now() - timedelta(days=7)).afirst()
    if not view:
        raise ValueError('Выдача устарела. Выполните новый поиск.')
    row = await sync_to_async(service.follow)(view.user_id, view.chat_id, service.view_query(view, source), int(interval))
    await callback.message.edit_text('✅ Слежу за «' + escape(query_title(row.filters)[:500]) +
        f'».\nПроверка каждые {row.interval_minutes} мин. Новые объявления пришлю сюда.', reply_markup=detail_keyboard(row))
    await refresh_view(callback.message, view)


@router.callback_query(F.data.startswith('track:'))
async def tracking_callback(callback, state: FSMContext):
    await callback.answer()
    await state.clear()
    try:
        parts = callback.data.split(':')
        action = parts[1]
        if action == 'add':
            await add_from_view(callback, parts)
            return
        if action == 'list':
            await show_list(callback.message, callback.from_user.id, int(parts[2]), edit=True)
            return
        row = await sync_to_async(service.owned)(int(parts[2]), callback.from_user.id, callback.message.chat.id)
        if not row:
            raise ValueError('Этот запрос недоступен или уже удалён.')
        if action == 'frequency':
            await callback.message.edit_text('⏱ Как часто проверять «' + escape(query_title(row.filters)[:500]) + '»?',
                reply_markup=interval_keyboard(f'track:interval:{row.pk}', row.interval_minutes))
            return
        if action == 'interval':
            row = await sync_to_async(service.change_interval)(row.pk, row.user_id, row.chat_id, int(parts[3]))
        elif action in ('pause', 'resume'):
            row = await sync_to_async(service.set_enabled)(row.pk, row.user_id, row.chat_id, action == 'resume')
        elif action == 'delete':
            await callback.message.edit_text('Удалить отслеживание «' + escape(query_title(row.filters)[:500]) + '»?',
                reply_markup=Keyboard(inline_keyboard=[[Button(text='Удалить', callback_data=f'track:remove:{row.pk}'),
                    Button(text='Отмена', callback_data=f'track:open:{row.pk}')]]))
            return
        elif action == 'remove':
            await sync_to_async(service.remove)(row.pk, row.user_id, row.chat_id)
            await show_list(callback.message, row.user_id, edit=True)
            return
        elif action == 'results':
            from bot.handlers.result_browser import open_query_browser
            await open_query_browser(callback.message, row.user_id, row.filters, row.search_job_id)
            return
        elif action != 'open':
            raise ValueError('Недоступная кнопка.')
        if not row:
            raise ValueError('Запрос уже удалён.')
        row = await sync_to_async(service.owned)(row.pk, callback.from_user.id, callback.message.chat.id)
        if not row:
            raise ValueError('Запрос уже удалён.')
        await callback.message.edit_text(detail_text(row), reply_markup=detail_keyboard(row))
    except ValidationError:
        await callback.message.answer('Выдача недоступна. Выполните новый поиск.')
    except (ValueError, IndexError, TypeError) as error:
        await callback.message.answer(escape(str(error) or 'Недоступная кнопка. Откройте «Отслеживание» заново.'))
    except TelegramBadRequest as error:
        if 'message is not modified' not in str(error):
            await callback.message.answer('Откройте «Отслеживание» заново.')
    except TelegramRetryAfter as error:
        await callback.message.answer(f'Повторите через {error.retry_after} с.')
