from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from bot.keyboards.inline.filters_kb import filters_keyboard
from bot.services.filters_service import get_user_filters

import logging
logger = logging.getLogger(__name__)

router = Router()

@router.message(Command("filters"))
async def filters_command(message: Message, state: FSMContext):
    from bot.handlers.text_search import open_filters
    await open_filters(message,state)
