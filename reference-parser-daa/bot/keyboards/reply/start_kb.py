from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

start_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="Включить уведомления"),
            KeyboardButton(text="Выключить уведомления"),
        ],
        [
            KeyboardButton(text="Фильтры"),
            KeyboardButton(text="Очистить выбор"),
        ],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
    is_persistent=True,
    input_field_placeholder="Выберите действие из меню",
    selective=False
)
