from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def check_filters_keyboard():
    inline_keyboard = []
    
    buttons = [
        InlineKeyboardButton(text="🔎 Найти машины", callback_data="search:run"),
        InlineKeyboardButton(text="Редактировать", callback_data="filters_edit"), 
    ]
    inline_keyboard.append(buttons)
    inline_keyboard.append([InlineKeyboardButton(text='Следить за новыми',callback_data='start_filtering')])
    
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
