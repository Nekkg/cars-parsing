"""Compatibility handlers for keyboards sent before saved-query tracking."""
from aiogram import Router, F
from asgiref.sync import sync_to_async
from bot.handlers.tracking import offer_current
from bot.keyboards.reply.start_kb import start_keyboard
from bot.services.tracking_service import stop_user

router = Router()


@router.message(F.text.in_({'Включить уведомления', 'Включить мониторинг'}))
async def start_monitoring_handler(message):
    await offer_current(message, message.from_user.id)


@router.message(F.text.in_({'Выключить уведомления', 'Выключить мониторинг'}))
async def stop_monitoring_handler(message):
    await sync_to_async(stop_user)(message.from_user.id)
    await message.answer('⏸ Все отслеживания поставлены на паузу.', reply_markup=start_keyboard)


@router.callback_query(F.data.startswith('notifications:'))
async def notification_button(callback):
    await callback.answer()
    if callback.data == 'notifications:on':
        await offer_current(callback.message, callback.from_user.id)
    else:
        await sync_to_async(stop_user)(callback.from_user.id)
        await callback.message.answer('⏸ Все отслеживания поставлены на паузу.', reply_markup=start_keyboard)
