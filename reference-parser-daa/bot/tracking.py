"""One scheduler and durable sender for all users' saved searches."""
import asyncio
import logging
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from asgiref.sync import sync_to_async
from django.db import close_old_connections
from bot.handlers.result_browser import media_for, remember_photo, forget_photo
from bot.result_presentation import caption, clean
from bot.services import tracking_service as service

logger = logging.getLogger(__name__)


async def deliver_one(bot):
    row = await sync_to_async(service.claim_delivery)()
    if not row:
        return False
    lease = row.lease_until
    try:
        if not await sync_to_async(service.still_allowed)(row.pk, lease):
            return True
        from bot.handlers.tracking import query_title, detail_keyboard
        header = '🔔 Новое по запросу «' + clean(query_title(row.subscription.filters), 90) + '»\n\n'
        media, key, available = await media_for(row.advertisement, 0)
        text = header + caption(row.advertisement, available)
        if not await sync_to_async(service.still_allowed)(row.pk, lease):
            return True
        async def send_photo():
            return await bot.send_photo(chat_id=row.chat_id, photo=media, caption=text, parse_mode='HTML',
                                        reply_markup=detail_keyboard(row.subscription, notification=True))
        if available and len(text.encode('utf-16-le')) // 2 <= 1024:
            try:
                message = await send_photo()
            except TelegramBadRequest as error:
                expired = any(word in str(error).lower() for word in ('wrong file','file identifier','file_id','file reference'))
                if not expired or not forget_photo(key):
                    raise
                media,key,available = await media_for(row.advertisement,0)
                if available:
                    message = await send_photo()
                else:
                    text = header + caption(row.advertisement,False)
                    message = await bot.send_message(chat_id=row.chat_id,text=text,parse_mode='HTML',
                        reply_markup=detail_keyboard(row.subscription,notification=True),disable_web_page_preview=True)
            if available:
                remember_photo(key,message)
        else:
            await bot.send_message(chat_id=row.chat_id, text=text, parse_mode='HTML',
                                   reply_markup=detail_keyboard(row.subscription, notification=True), disable_web_page_preview=True)
        await sync_to_async(service.sent)(row.pk, lease)
    except TelegramForbiddenError:
        await sync_to_async(service.stop_user)(row.user_id)
    except TelegramRetryAfter as error:
        await sync_to_async(service.retry)(row.pk, lease, error.retry_after)
    except Exception as error:
        await sync_to_async(service.retry)(row.pk, lease, min(300, 15 * 2 ** min(row.attempts, 5)))
        logger.warning('Tracking delivery deferred type=%s', type(error).__name__)
    return True


def scheduler_tick():
    close_old_connections()
    service.schedule_due_checks()
    service.queue_new_ads()


async def scheduler_loop():
    while True:
        try:
            await sync_to_async(scheduler_tick)()
        except Exception as error:
            logger.warning('Tracking scheduler deferred type=%s', type(error).__name__)
        await asyncio.sleep(5)


async def sender_loop(bot):
    while True:
        try:
            delivered = await deliver_one(bot)
        except Exception as error:
            logger.warning('Tracking sender deferred type=%s', type(error).__name__)
            delivered = False
        await asyncio.sleep(1.1 if delivered else 3)


async def tracking_loop(bot):
    logger.info('Saved search tracking started')
    await asyncio.gather(scheduler_loop(), sender_loop(bot))
