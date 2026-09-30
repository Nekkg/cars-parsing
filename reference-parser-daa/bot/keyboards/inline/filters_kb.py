from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# Функция для создания клавиатуры с фильтрами
def filters_keyboard():
    # Строки с фильтрами
    filters = [
        ['Площадка', 'Цена', 'Марка'],
        ['Двигатель', 'Пробег', 'Коробка'],
        ['Кол-во владельцев', 'Состояние', 'Продавец'],
        ['Город', 'Год выпуска', 'Тип кузова'],
        ['Цвет кузова', 'Привод', 'Руль']
    ]
    
    # Формируем inline клавиатуру
    inline_keyboard = [[InlineKeyboardButton(text='🔎 Найти машины', callback_data='search:run')]]
    
    # Добавление кнопок на клавиатуру
    for filter_row in filters:
        buttons = [
            InlineKeyboardButton(
                text=filter_name, 
                callback_data=f"filter_{filter_name}"
            ) 
            for filter_name in filter_row
        ]
        inline_keyboard.append(buttons)
    
    # Добавляем кнопку "Применить фильтры"
    inline_keyboard.append([
        InlineKeyboardButton(text="✅ Проверить свои фильтры", callback_data="check_filters")
    ])
    inline_keyboard.append([InlineKeyboardButton(text='Очистить выбор',callback_data='selection:clear')])
    inline_keyboard.append([InlineKeyboardButton(text='Включить уведомления',callback_data='notifications:on'),
                            InlineKeyboardButton(text='Выключить уведомления',callback_data='notifications:off')])
    
    # Создание и возврат клавиатуры
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
