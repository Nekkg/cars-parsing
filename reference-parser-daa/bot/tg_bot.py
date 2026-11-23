import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
os.environ.update({'DJANGO_ALLOW_ASYNC_UNSAFE': "true"})

import django
django.setup()

import asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.bot import DefaultBotProperties
from aiogram.types import BotCommand, BotCommandScopeDefault
from core.network import telegram_session

from dotenv import load_dotenv

from bot.commands import start_command, monitor_commands, filters_command
from bot.handlers import monitoring_handler, filters_handler
from bot.handlers import text_search, result_browser, tracking
from bot.tracking import tracking_loop
from bot.live_results import live_results_loop
from bot.handlers import vehicle_search
from bot.callbacks import filters_callback, filters_check_callback
from bot.ads_sender import main as start_ads_sender
from bot.broadcast_sender import broadcast_loop as start_broadcast_sender

import logging
from core.paths import BOT_LOG_FILE

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BOT_LOG_FILE, mode='a')  # Локальный журнал в runtime/logs/bot.log
    ]
)

logger = logging.getLogger(__name__)


load_dotenv(encoding='utf-8')

async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="car", description="Марка, модель и поколение"),
        BotCommand(command="search", description="Запустить поиск на площадках"),
        BotCommand(command="results", description="Результаты поиска"),
        BotCommand(command="status", description="Состояние источников"),
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="tracking", description="Отслеживаемые машины и частота проверки"),
        BotCommand(command="filters", description="Настроить фильтры"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

async def main():
    bot = None
    try:
        bot = Bot(os.getenv('TELEGRAM_BOT_TOKEN'), session=telegram_session(), default=DefaultBotProperties(parse_mode='HTML'))
        dp = Dispatcher()
        
        # Устанавливаем команды бота
        await set_commands(bot)
        
        dp.include_routers(
            tracking.router,
            text_search.router,
            result_browser.router,
            vehicle_search.router,
            start_command.router,
            monitor_commands.router,
            filters_command.router,
            monitoring_handler.router,
            filters_handler.router,
            filters_callback.router,
            filters_check_callback.router,
        )
        
        await bot.delete_webhook(drop_pending_updates=False)
        await asyncio.gather(
            dp.start_polling(bot),
            live_results_loop(bot),
            tracking_loop(bot),
            start_broadcast_sender(bot),
            start_ads_sender(bot),
        )
    finally:
        if bot is not None:
            await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
