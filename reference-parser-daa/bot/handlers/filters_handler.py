from aiogram.types import Message
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from keyboards.inline.filters_kb import filters_keyboard
from services.filters_service import get_user_filters

import logging
logger = logging.getLogger(__name__)

router = Router()

@router.message(F.text == "Фильтры")
async def enable_filter_callback(message: Message, state: FSMContext):
    from bot.handlers.text_search import open_filters
    await open_filters(message,state)
