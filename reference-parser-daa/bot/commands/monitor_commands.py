"""Old notification commands remain aliases for saved-query tracking."""
from aiogram import Router
from aiogram.filters import Command
from asgiref.sync import sync_to_async
from bot.handlers.tracking import offer_current
from bot.keyboards.reply.start_kb import start_keyboard
from bot.services.tracking_service import stop_user

router = Router()


@router.message(Command('monitor_on', 'notifications_on'))
async def start_monitoring_command(message):
    await offer_current(message, message.from_user.id)


@router.message(Command('monitor_off', 'notifications_off'))
async def stop_monitoring_command(message):
    await sync_to_async(stop_user)(message.from_user.id)
    await message.answer('⏸ Все отслеживания поставлены на паузу.', reply_markup=start_keyboard)
