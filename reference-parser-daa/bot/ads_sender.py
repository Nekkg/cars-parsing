import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
os.environ.update({'DJANGO_ALLOW_ASYNC_UNSAFE': "true"})

import django
django.setup()

import asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from asgiref.sync import sync_to_async
from django.db import close_old_connections

from users.models import User
from services.remove_user_service import remove_user_from_db
from ads.models import Delivery
from ads.search import results, snapshot
from core.network import telegram_session

import logging
from core.paths import BOT_LOG_FILE


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BOT_LOG_FILE, mode='a')
    ]
)

TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
bot = Bot(token=TOKEN, session=telegram_session())


def escape_md_v2(text: str) -> str:
    """
    Экранирует все спецсимволы, требуемые MarkdownV2.
    Список символов: _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    text = '' if text is None else str(text)
    if text in ('unknown','None'): text = '—'
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for ch in special_chars:
        text = text.replace(ch, f"\\{ch}")
    if text == "unknown":
        text = ""
    return text

def bold_md(text: str) -> str:
    """Оформляет текст как жирный (MarkdownV2)."""
    return f"*{text}*"

def italic_md(text: str) -> str:
    """Оформляет текст как курсив (MarkdownV2)."""
    return f"_{text}_"

def link_md(title: str, url: str) -> str:
    """Оформляет ссылку (MarkdownV2)."""
    return f"[{title}]({url})"

async def send_ad_to_user(user_id, ad):
    """
    Отправляет объявление пользователю. Динамические данные проходят экранирование,
    затем форматируются с помощью функций bold_md, italic_md и link_md.
    """
    # Экранируем и форматируем все динамические поля
    name = escape_md_v2(ad.get('name', ''))
    year = escape_md_v2(str(ad.get('year', '')))
    ad_link = escape_md_v2(ad.get('link', ''))
    price = escape_md_v2(str(ad.get('price', '')))
    city = escape_md_v2(ad.get('city', '').title())
    brand = escape_md_v2(ad.get('brand', '').title())
    model = escape_md_v2(ad.get('model', '').title())
    engine = escape_md_v2(ad.get('engine', ''))
    mileage = escape_md_v2(str(ad.get('mileage', '')))
    gearbox = escape_md_v2(ad.get('gearbox', ''))
    drive = escape_md_v2(ad.get('drive', ''))
    steering = escape_md_v2(ad.get('steering', ''))
    body_type = escape_md_v2(ad.get('body_type', ''))
    color = escape_md_v2(ad.get('color', ''))
    owners = escape_md_v2(str(ad.get('owners', '')))
    condition = escape_md_v2(ad.get('condition', ''))
    seller = escape_md_v2(ad.get('seller', ''))
    ad_type = escape_md_v2(ad.get('ad_type', ''))
    platform = escape_md_v2(ad.get('platform', ''))
    
    image = ad.get('image', '')

    # Формируем текстовое сообщение с применением MarkdownV2
    text_msg = (
        f"{bold_md(name)}\n\n"
        f"{bold_md('💰 Цена:')} {price} ₽\n"
        f"{bold_md('📍 Город:')} {city}\n"
        f"{bold_md('🚗 Марка:')} {brand}\n"
        f"{bold_md('🚗 Модель:')} {model}\n"
        f"{bold_md('📅 Год:')} {year}\n"
        f"{bold_md('📱 Площадка:')} {platform}\n\n"
        f"{bold_md('🔧 Параметры:')}\n"
        f"{italic_md('⛽ Двигатель:')} {engine}\n"
        f"{italic_md('📏 Пробег:')} {mileage} км\n"
        f"{italic_md('⚙️ Коробка:')} {gearbox}\n"
        f"{italic_md('🔧 Привод:')} {drive}\n"
        f"{italic_md('🚗 Руль:')} {steering}\n"
        f"{italic_md('🏷 Тип кузова:')} {body_type}\n"
        f"{italic_md('🎨 Цвет:')} {color}\n"
        f"{italic_md('👤 Владельцев:')} {owners}\n"
        f"{italic_md('🏷 Состояние:')} {condition}\n\n"
        f"{bold_md('🏢 Продавец:')} {seller}\n"
        f"{link_md('Ссылка на объявление', ad_link)}\n"
    )

    try:
        if ad["image"] is None or ad["image"] == "":
            await bot.send_message(chat_id=user_id, text=text_msg, parse_mode="MarkdownV2")
            return True
        from bot.photo_delivery import download_photo
        from aiogram.types import BufferedInputFile
        photo = await asyncio.to_thread(download_photo,image,ad.get('link',''))
        await bot.send_photo(chat_id=user_id, photo=BufferedInputFile(photo,filename='car.jpg'),
                             caption=text_msg if len(text_msg) <= 1024 else None, parse_mode="MarkdownV2")
        if len(text_msg) > 1024:
            await bot.send_message(chat_id=user_id,text=text_msg,parse_mode='MarkdownV2')
        return True
    except TelegramForbiddenError:
        logging.info(f"Пользователь {user_id} заблокировал бота. Удаляем его из базы.")
        await remove_user_from_db(user_id)  # Удалить или пометить пользователя
    except TelegramRetryAfter as e:
        logging.error(f"⏳ Превышен лимит запросов к Telegram API: ожидание {e.retry_after} сек.")
        await asyncio.sleep(e.retry_after)
    except Exception as e:
        logging.error(f"❌ Ошибка отправки пользователю {user_id}: {e}, объявление: {ad}")
        await bot.send_message(chat_id=user_id, text=text_msg, parse_mode="MarkdownV2")
        return True



async def main():
    while True:
        try:
            pairs = await sync_to_async(pending_deliveries)()
            for user_id, ad in pairs:
                if not await sync_to_async(notification_allowed)(user_id,ad): continue
                if await send_ad_to_user(user_id, ad.__dict__):
                    await Delivery.objects.aget_or_create(user_id=user_id, advertisement=ad, price=ad.price)
                await asyncio.sleep(1.1)
        except Exception as error:
            logging.error('Notification cycle failed: %s', type(error).__name__)
        await asyncio.sleep(30)
        close_old_connections()


from ads.notifications import pending_deliveries, notification_allowed

if __name__ == "__main__":
    asyncio.run(main())
    logging.info("Ads sender is running...")
