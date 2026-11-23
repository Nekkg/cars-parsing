from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from bot.services.filters_service import update_user_filter, get_user_filters, get_user_filter_values, clear_user_filter, get_user_filters_explain, update_user_location
from bot.keyboards.inline.check_filters_kb import check_filters_keyboard
from bot.keyboards.inline.filters_mapping_kb import filter_keyboard, filter_keyboard_back_button, city_keyboard
from bot.keyboards.inline.filters_kb import filters_keyboard
from .filter_mapping import FILTER_MAPPING_LANGUAGE, FILTER_MAPPING_DESCRIPTION

from html import escape
from ads.geography import parse_city_input
from users.models import UserFilters
from bot.handlers.text_search import MENU_TEXTS
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard

CITY_HINT = ('Выберите города кнопками или напишите город и радиус сообщением.\n'
             'Например: Самара, Самара +100, Самара +200 км, мск +200.\n'
             'Для радиуса нужен один город. Расстояние считается по прямой.')


def city_panel_text(values, radius_km=0):
    selection = ', '.join(values) or 'не выбран'
    if radius_km:
        selection += f' +{radius_km} км'
    return CITY_HINT + '\n\nГород: ' + escape(selection)


async def activate_city_input(state, message):
    await state.set_state(FilterStates.entering_city)
    await state.update_data(city_panel_id=message.message_id, city_panel_chat=message.chat.id,
                            city_choices=[], city_choice_message=None)


async def current_city_origin(user_id):
    filters, _ = await UserFilters.objects.aget_or_create(user_id=user_id)
    current = filters.city
    origin = None
    if len(current) == 1:
        origin = filters.location if filters.location.get('name') == current[0] else current[0]
    return current, origin

async def apply_city_request(message, state, user_id, parsed):
    if parsed['city_choices']:
        sent = await message.answer('Уточните город:', reply_markup=Keyboard(inline_keyboard=[
            [Button(text=(city['name']+' · '+city['region'])[:90], callback_data=f'city:choose:{i}')]
            for i, city in enumerate(parsed['city_choices'])]))
        await state.update_data(city_choices=parsed['city_choices'], city_radius=parsed['radius_km'],
                                city_choice_message=sent.message_id)
        return
    await update_user_location(user_id, parsed['location'], parsed['radius_km'])
    data = await state.get_data()
    kwargs = dict(text=city_panel_text(parsed['city'], parsed['radius_km']),
                  reply_markup=city_keyboard(parsed['city'], parsed['radius_km']), parse_mode='HTML')
    try:
        await message.bot.edit_message_text(chat_id=data.get('city_panel_chat', message.chat.id),
                                           message_id=data['city_panel_id'], **kwargs)
    except (TelegramBadRequest, KeyError) as error:
        if 'message is not modified' not in str(error).lower():
            sent = await message.answer(**kwargs)
            await state.update_data(city_panel_id=sent.message_id, city_panel_chat=message.chat.id)
    await state.update_data(city_choices=[], city_choice_message=None)
    await state.set_state(FilterStates.entering_city)

import logging
logger = logging.getLogger(__name__)

class FilterStates(StatesGroup):
    entering_value = State()
    entering_city = State()

router = Router()

@router.callback_query(F.data.startswith("filter_"))
async def filter_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    filter_name_rus = callback.data.replace("filter_", "")  # Русское название
    filter_name = FILTER_MAPPING_LANGUAGE.get(filter_name_rus)  # Перевод в поле модели
    filter_description = FILTER_MAPPING_DESCRIPTION.get(filter_name_rus)
    
    if not filter_name:
        await callback.message.answer("❌ Ошибка: фильтр не найден.")
        return
    
    current_values = await get_user_filter_values(callback.from_user.id, filter_name)
    
    # Если это фильтр города, показываем специальную клавиатуру
    if filter_name_rus == "Город":
        await callback.answer()
        filters, _ = await UserFilters.objects.aget_or_create(user_id=callback.from_user.id)
        await callback.message.edit_text(city_panel_text(current_values, filters.radius_km),
                                         reply_markup=city_keyboard(current_values, filters.radius_km), parse_mode='HTML')
        await activate_city_input(state, callback.message)
        return
    keyboard = filter_keyboard(filter_name_rus, current_values, page=1)
    
    sent_message = await callback.message.edit_text(
        f"Введите значение для {filter_name_rus}\n"
        f"Формат: «{filter_description}»",
        reply_markup=keyboard if keyboard else filter_keyboard_back_button(filter_name=filter_name_rus),
    )

    if not keyboard:
        await state.update_data(sent_message=sent_message)
        await state.update_data(selected_filter=filter_name)  # Сохраняем выбранный фильтр в state
        await state.update_data(selected_filter_rus=filter_name_rus)  # Сохраняем выбранный фильтр в state
        await state.set_state(FilterStates.entering_value)  # Устанавливаем состояние


@router.message(FilterStates.entering_value)
async def save_filter_value(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await state.get_data()  # Получаем данные из state
    filter_name = user_data.get("selected_filter")  # Достаём сохранённый фильтр
    filter_name_rus = user_data.get("selected_filter_rus")
    sented_message = user_data.get("sent_message")  # Достаём отправленное сообщение
    filter_value = message.text
    if filter_name in ('year','mileage','owners'):
        import re
        from ads.filter_values import bounds
        if re.search(r'[a-zа-я]',filter_value or '',re.I):
            from bot.handlers.text_search import query_message
            await query_message(message,state)
            return
        try:
            lo,hi=bounds(filter_value)
            if lo is None and hi is None: raise ValueError('Введите год или диапазон чисел.')
            if filter_name=='year' and '-' not in filter_value:
                lo=hi
            if filter_name=='year' and any(v is not None and not 1900 <= v <= 2100 for v in (lo,hi)):
                raise ValueError('Укажите год из четырёх цифр, например 2018.')
            filter_value=f'{lo if lo is not None else ""}-{hi if hi is not None else ""}'
        except (ValueError,TypeError) as error:
            await message.answer(str(error));return
    if sented_message:
        try: await sented_message.delete()
        except TelegramBadRequest: pass

    await message.delete() # Удаляем сообщение с введённым значением
    if not filter_name:
        await message.answer("❌ Ошибка: фильтр не найден.")
        return
    
    # Получаем информацию о поле из модели
    from users.models import UserFilters
    model_field = UserFilters._meta.get_field(filter_name)
    
    # Проверяем, является ли поле массивом
    from core.fields import ArrayField
    if isinstance(model_field, ArrayField):
        # Для полей-массивов разделяем значения по запятой
        if "," in filter_value:
            values = [v.strip() for v in filter_value.split(",")]
            await update_user_filter(user_id, filter_name, values)
        else:
            # Для одиночного значения создаём список с одним элементом
            await update_user_filter(user_id, filter_name, [filter_value])
    else:
        # Для обычных полей сохраняем как есть
        await update_user_filter(user_id, filter_name, filter_value)
    
    await message.answer(f"✅ Фильтр {filter_name_rus} обновлён: {filter_value}", reply_markup=filter_keyboard_back_button(filter_name=filter_name_rus))
    
    await state.clear()  # Очищаем состояние


@router.message(FilterStates.entering_city, F.text, ~F.text.startswith('/'), ~F.text.in_(MENU_TEXTS))
async def save_city_value(message: Message, state: FSMContext):
    try:
        _, origin = await current_city_origin(message.from_user.id)
        parsed = parse_city_input(message.text, origin)
    except ValueError as error:
        await message.answer(escape(str(error)), parse_mode='HTML')
        return
    await apply_city_request(message, state, message.from_user.id, parsed)


@router.callback_query(F.data.startswith('city:'))
async def city_action(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    _, action, raw = callback.data.split(':')
    try:
        value = int(raw)
        if action == 'choose':
            choices = data.get('city_choices', [])
            if callback.message.message_id != data.get('city_choice_message') or not 0 <= value < len(choices):
                raise ValueError('Этот выбор устарел. Введите город заново.')
            city = choices[value]
            parsed = dict(city=[city['name']], location=city, radius_km=data['city_radius'], city_choices=[])
            await callback.answer()
            await apply_city_request(callback.message, state, callback.from_user.id, parsed)
            await callback.message.edit_reply_markup(reply_markup=None)
        elif action == 'radius' and value in (0, 100, 200, 500):
            if data.get('city_panel_id') != callback.message.message_id:
                raise ValueError('Этот выбор устарел. Откройте фильтр города заново.')
            current, origin = await current_city_origin(callback.from_user.id)
            if not value:
                await update_user_filter(callback.from_user.id, 'city', current)
                await callback.answer()
                try:
                    await callback.message.edit_text(city_panel_text(current), reply_markup=city_keyboard(current), parse_mode='HTML')
                except TelegramBadRequest as error:
                    if 'message is not modified' not in str(error).lower():
                        raise
                return
            if len(current) != 1:
                raise ValueError('Для радиуса выберите один город или напишите: Самара +200.')
            parsed = parse_city_input(f'+{value}', origin)
            await callback.answer()
            await apply_city_request(callback.message, state, callback.from_user.id, parsed)
        else:
            raise ValueError('Недоступная кнопка. Откройте фильтр города заново.')
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)

@router.callback_query(F.data.startswith("toggle_filter_"))
async def save_filter_value_inline(callback: CallbackQuery):
    try:
        filter_name_rus, value, _ = callback.data.split("_")[2:]
        user_id = callback.from_user.id
        filter_name_eng = FILTER_MAPPING_LANGUAGE.get(filter_name_rus)  # Достаём сохранённый фильтр
        
        # Получаем текущие значения фильтра
        current_values = await get_user_filter_values(user_id, filter_name_eng)
        
        # Если значение уже выбрано - удаляем его, иначе добавляем
        if value in current_values:
            # Удаляем значение из списка
            if isinstance(current_values, list):
                current_values.remove(value)
        else:
            # Добавляем значение в список
            if not current_values:
                current_values = [value]
            elif isinstance(current_values, list):
                current_values.append(value)
            else:
                current_values = [current_values, value]
        
        await update_user_filter(user_id, filter_name_eng, current_values)  # Сохраняем в БД
        
        keyboard = filter_keyboard(filter_name_rus, current_values, page=1)
        
        await callback.message.edit_reply_markup(reply_markup=keyboard)  # Обновляем клавиатуру
        
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data.startswith("toggle_city_"))
async def toggle_city(callback: CallbackQuery, state: FSMContext):
    try:
        city = callback.data.replace("toggle_city_", "")
        user_id = callback.from_user.id
        logger.info(f"Toggle city {city} for user {user_id}")
        
        # Получаем текущие значения фильтра
        current_values = await get_user_filter_values(user_id, "city")
        logger.info(f"Current city values: {current_values}")
        
        # Если город уже выбран - удаляем его, иначе добавляем
        if city in current_values:
            # Удаляем город из списка
            if isinstance(current_values, list):
                current_values.remove(city)
            else:
                current_values = []
        else:
            # Добавляем город в список
            if not current_values:
                current_values = [city]
            elif isinstance(current_values, list):
                current_values.append(city)
            else:
                current_values = [current_values, city]
        
        logger.info(f"Updated city values: {current_values}")
        
        # Сохраняем обновленный список городов
        await update_user_filter(user_id, "city", current_values)
        
        # Обновляем клавиатуру
        keyboard = city_keyboard(current_values)
        await callback.message.edit_text(city_panel_text(current_values), reply_markup=keyboard, parse_mode='HTML')
        await activate_city_input(state, callback.message)
        
        # Показываем уведомление
        await callback.answer(f"{'✅ Город добавлен' if city in current_values else '❌ Город удален'}: {city}")
        
    except Exception as e:
        logger.error(f"Error in toggle_city: {e}")
        await callback.message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data == "enter_custom_city")
async def enter_custom_city(callback: CallbackQuery, state: FSMContext):
    await callback.answer('Напишите город и, при необходимости, +200 км')
    await activate_city_input(state, callback.message)
    await callback.message.answer(CITY_HINT)

@router.callback_query(F.data == "check_filters")
async def check_filters(callback: CallbackQuery):
    user_id = callback.from_user.id
    filters_text = await get_user_filters(user_id)
    keyboard = check_filters_keyboard()
    await callback.message.edit_text(filters_text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("clear_filter_"))
async def clear_filter(callback: CallbackQuery, state: FSMContext):
    try:
        user_id = callback.from_user.id
        filter_name_rus = callback.data.split("_")[2]
        filter_name = FILTER_MAPPING_LANGUAGE.get(filter_name_rus)  # Перевод в поле модели
        
        # Получаем текущие значения перед очисткой
        current_values = await get_user_filter_values(user_id, filter_name)
        logger.info(f"Clearing filter {filter_name} for user {user_id}. Current values: {current_values}")
        
        # Очищаем фильтр
        await clear_user_filter(user_id, filter_name)
        
        # Если это фильтр города, показываем специальную клавиатуру
        if filter_name_rus == "Город":
            panel = callback.message
            try:
                await panel.edit_text(city_panel_text([]), reply_markup=city_keyboard([]), parse_mode='HTML')
            except TelegramBadRequest as error:
                if 'message is not modified' not in str(error).lower():
                    panel = await callback.message.answer(city_panel_text([]), reply_markup=city_keyboard([]), parse_mode='HTML')
            await activate_city_input(state, panel)
            await callback.answer('✅ Фильтр по городам и радиус очищены')
            return
        await state.clear()
        # Для остальных фильтров - стандартное поведение
        filters_text = await get_user_filters(callback.from_user.id)
        keyboard = filters_keyboard()
        await callback.message.edit_text(filters_text, reply_markup=keyboard, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error in clear_filter: {e}")
        await callback.message.answer(f"❌ Ошибка при очистке фильтра: {e}")
        # Возвращаемся к меню фильтров
        filters_text = await get_user_filters(callback.from_user.id)
        keyboard = filters_keyboard()
        await callback.message.edit_text(filters_text, reply_markup=keyboard, parse_mode="HTML")
