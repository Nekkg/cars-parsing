from aiogram import Router, F
from aiogram.types import Message
from bot.keyboards.reply.start_kb import start_keyboard

from services.monitoring_switch_service import enable_monitoring, disable_monitoring

import logging
logger = logging.getLogger(__name__)

router = Router()

@router.message(F.text.in_({"Включить уведомления","Включить мониторинг"}))
async def start_monitoring_handler(message: Message):
    user_id = message.from_user.id
    await enable_monitoring(user_id=user_id)
    logger.info(f"Monitoring is enabled for user {user_id}")
    await message.answer("✅ Уведомления включены",reply_markup=start_keyboard)


@router.message(F.text.in_({"Выключить уведомления","Выключить мониторинг"}))
async def stop_monitoring_handler(message: Message):
    user_id = message.from_user.id
    await disable_monitoring(user_id=user_id)
    logger.info(f"Monitoring is disabled for user {user_id}")
    await message.answer("❌ Уведомления выключены",reply_markup=start_keyboard)


@router.callback_query(F.data.startswith('notifications:'))
async def notification_button(callback):
    await callback.answer()
    if callback.data=='notifications:on':
        await enable_monitoring(callback.from_user.id)
        text='✅ Уведомления включены'
    else:
        await disable_monitoring(callback.from_user.id)
        text='❌ Уведомления выключены'
    await callback.message.answer(text,reply_markup=start_keyboard)
