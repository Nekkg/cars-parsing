from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext
from bot.keyboards.inline.filters_kb import filters_keyboard
from bot.services.filters_service import get_user_filters
from bot.services.monitoring_switch_service import enable_monitoring

import logging
logger = logging.getLogger(__name__)

router = Router()

@router.callback_query(F.data == "filters_edit")
async def edit_filter_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    from bot.handlers.text_search import open_filters
    await open_filters(callback.message,state,callback.from_user.id)
    
@router.callback_query(F.data == "start_filtering")
async def start_filtering_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    await enable_monitoring(user_id=user_id)
    logger.info(f"Monitoring is enabled for user {user_id}")
    filters_text = "Уведомления включены!"

    await callback.message.edit_text(filters_text)
