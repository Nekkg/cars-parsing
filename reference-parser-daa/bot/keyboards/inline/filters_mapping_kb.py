from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from ads.filter_schema import FILTER_OPTIONS


FILTER_MAPPING_KB = {
    "Площадка": ["avito", "drom", "autoru"],
    "Цена": None,
    "Марка": [
        "Lada (ВАЗ)",
        "Audi",
        "BMW",
        "Belgee",
        "Changan",
        "Chery",
        "Chevrolet",
        "Citroen",
        "Exeed",
        "Ford",
        "GAC",
        "Geely",
        "Haval",
        "Honda",
        "Hyundai",
        "Jaecoo",
        "Knewstar",
        "Land Rover",
        "Lexus",
        "LiXiang",
        "Mazda",
        "Mercedes-Benz",
        "Mitsubishi",
        "Nissan",
        "Omoda",
        "Opel",
        "Peugeot",
        "Porsche",
        "Renault",
        "Skoda",
        "Toyota",
        "Volkswagen",
        "Volvo"
    ],
    "Двигатель": list(FILTER_OPTIONS['engine']),
    "Пробег": None,
    "Коробка": list(FILTER_OPTIONS['gearbox']),
    "Кол-во владельцев": None,
    "Состояние": list(FILTER_OPTIONS['condition']),
    "Продавец": list(FILTER_OPTIONS['seller']),
    "Город": None,
    "Год выпуска": None,
    "Тип кузова": list(FILTER_OPTIONS['body_type']),
    "Цвет кузова": None,
    "Привод": list(FILTER_OPTIONS['drive']),
    "Руль": list(FILTER_OPTIONS['steering']),
}

# 36 крупнейших городов России
POPULAR_CITIES = [
    "Москва", "Санкт-Петербург", "Новосибирск", 
    "Екатеринбург", "Казань", "Нижний Новгород", 
    "Челябинск", "Самара", "Омск", 
    "Ростов-на-Дону", "Уфа", "Красноярск", 
    "Воронеж", "Пермь", "Волгоград", 
    "Краснодар", "Саратов", "Тюмень", 
    "Тольятти", "Ижевск", "Барнаул", 
    "Ульяновск", "Иркутск", "Хабаровск", 
    "Ярославль", "Владивосток", "Махачкала", 
    "Томск", "Оренбург", "Кемерово", 
    "Новокузнецк", "Рязань", "Астрахань", 
    "Пенза", "Липецк", "Киров"
]
     
def filter_keyboard(filter_name: str, selected_values=None, page=1) -> InlineKeyboardMarkup:
    if selected_values is None:
        selected_values = []
    inline_keyboard = []
    options = FILTER_MAPPING_KB.get(filter_name)
    if options:
        # Проходим по всем опциям и добавляем их в клавиатуру
        row = []  # Список для одной строки
        for idx, option in enumerate(options):
            # Если значение выбрано, добавляем галочку, иначе – крестик (или другой символ)
            if option in selected_values:
                text = f"✅ {option}"
            else:
                text = f"{option}"

            row.append(InlineKeyboardButton(
                text=text,
                callback_data=f"toggle_filter_{filter_name}_{option}_1"  # page=1 всегда
            ))

            # Если достигли 3 кнопок в строке, добавляем строку в клавиатуру и очищаем row
            if len(row) == 3:
                inline_keyboard.append(row)
                row = []  # Сброс строки

        # Добавляем оставшиеся кнопки, если их меньше 3
        if row:
            inline_keyboard.append(row)

        inline_keyboard.append([InlineKeyboardButton(
            text="Очистить фильтр",
            callback_data=f"clear_filter_{filter_name}",
        )])
        # Кнопка "Назад" для завершения выбора
        inline_keyboard.append([InlineKeyboardButton(
            text="⬅️ Вернуться к фильтрам",
            callback_data="filters_edit",
        )])
        return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
    return None


def city_keyboard(selected_values=None, radius_km=0) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру с популярными городами"""
    if selected_values is None:
        selected_values = []
    
    inline_keyboard = []
    row = []
    
    # Добавляем кнопки с городами
    for i, city in enumerate(POPULAR_CITIES):
        # Если этот город уже выбран, добавляем галочку
        if city in selected_values:
            text = f"✅ {city}"
        else:
            text = f"{city}"
        
        row.append(InlineKeyboardButton(text=text, callback_data=f"toggle_city_{city}"))
        
        # Добавляем по 3 кнопки в ряд
        if (i + 1) % 3 == 0 or i == len(POPULAR_CITIES) - 1:
            inline_keyboard.append(row)
            row = []
    
    inline_keyboard.append([
        InlineKeyboardButton(text=('✓ ' if radius_km == km else '') + ('Только город' if km == 0 else f'+{km} км'),
                             callback_data=f'city:radius:{km}') for km in (0, 100, 200, 500)
    ])
    # Добавляем кнопку очистки и возврата к фильтрам
    inline_keyboard.append([InlineKeyboardButton(
        text="Очистить фильтр",
        callback_data="clear_filter_Город",
    )])
    
    inline_keyboard.append([InlineKeyboardButton(
        text="⬅️ Вернуться к фильтрам",
        callback_data="filters_edit",
    )])
    
    # Добавляем кнопку для ввода своего города
    inline_keyboard.append([InlineKeyboardButton(
        text="✏️ Город и радиус",
        callback_data="enter_custom_city",
    )])
    
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def filter_keyboard_back_button(filter_name: str) -> InlineKeyboardMarkup:
    inline_keyboard = []
    inline_keyboard.append([InlineKeyboardButton(
            text="Очистить фильтр",
            callback_data=f"clear_filter_{filter_name}",
        )])
    inline_keyboard.append([InlineKeyboardButton(
            text="⬅️ Вернуться к фильтрам",
            callback_data="filters_edit",
    )])
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
