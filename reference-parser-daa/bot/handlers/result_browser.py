"""One editable photo card and a separately paginated list, persisted per message."""
import asyncio
import logging
from collections import OrderedDict
from datetime import timedelta
from weakref import WeakValueDictionary

from aiogram import Router,F
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile,InputMediaPhoto
from aiogram.exceptions import TelegramBadRequest,TelegramRetryAfter
from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.utils import timezone
from ads.models import Advertisement,ResultView
from ads.search import results,snapshot,result_order,enqueue,SORT_LABELS
from users.models import UserFilters
from bot.photo_delivery import download_photo,placeholder_photo
from bot.result_presentation import pictures,caption,keyboard,listing

router = Router()
logger = logging.getLogger(__name__)
_locks = WeakValueDictionary()
_photo_ids = OrderedDict()
_failed_photos = OrderedDict()


def lock_for(key):
    key=str(key).replace('-','')
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


@sync_to_async
def create_view(user_id,chat_id,query=None,search_job_id=None):
    if query is None:
        filters,_ = UserFilters.objects.get_or_create(user_id=user_id)
        data,sort_order,job_id = snapshot(filters),filters.sort_order,filters.search_job_id
    else:
        data,sort_order,job_id = query,'newest',search_job_id
    ads = results(data,sort_order)
    ads,_ = result_order(ads,['avito','autoru','drom'],sort_order)
    ResultView.objects.filter(user_id=user_id,created_at__lt=timezone.now()-timedelta(days=7)).delete()
    ResultView.objects.filter(user_id=user_id,live_until__isnull=False).update(live_until=None,collect_until=None)
    return ResultView.objects.create(user_id=user_id,chat_id=chat_id,filters=data,
        result_ids=[ad.pk for ad in ads],sort_order=sort_order,search_job_id=job_id,
        live_until=timezone.now()+timedelta(hours=1) if job_id else None,
        live_state={'seen_ids':[ad.pk for ad in ads]})


@sync_to_async
def view_rows(view):
    pairs = dict(Advertisement.objects.filter(pk__in=view.result_ids).values_list('pk','platform'))
    counts = {source:sum(value==source for value in pairs.values()) for source in ('avito','autoru','drom')}
    ids = [pk for pk in view.result_ids if pk in pairs and (view.platform=='all' or pairs[pk]==view.platform)]
    view.position = max(0,min(view.position,len(ids)-1))
    view.list_page = max(0,min(view.list_page,max(0,(len(ids)-1)//10)))
    return ids,counts


@sync_to_async
def is_following(view):
    from bot.services.tracking_service import view_query, fingerprint
    from users.models import TrackedSearch
    return TrackedSearch.objects.filter(user_id=view.user_id,enabled=True,
        fingerprint=fingerprint(view_query(view))).exists()


@sync_to_async
def request_photos(ad):
    from parsers.media_queue import request_gallery
    row = request_gallery(ad,retry=True)
    if row.finished_at:
        return 'Площадка пока не предоставила дополнительные фото. Откройте объявление по ссылке.' if row.error_code or not pictures(ad) else 'Все доступные фотографии уже загружены.'
    if row.error_code=='blocked': return 'Площадка ограничила доступ к фото. Повторим после паузы.'
    return 'Загружаю фотографии. Нажмите «Фото» ещё раз через несколько секунд.'


@sync_to_async
def prefetch_photos(ids, position):
    """Fetch the opened Auto.ru gallery and at most two next cars, once per ad."""
    from parsers.media_queue import request_gallery
    window = ids[position:position+3]
    rows = Advertisement.objects.in_bulk(window)
    pending = None
    for pk in window:
        ad = rows.get(pk)
        if not ad or ad.platform != 'autoru' or len(pictures(ad)) > 1:
            continue
        request = request_gallery(ad)
        if pk == window[0] and not request.finished_at:
            pending = pk
    return pending


def remember_photo(key,message):
    if key != 'placeholder' and getattr(message,'photo',None):
        _photo_ids[key] = message.photo[-1].file_id
        while len(_photo_ids)>512: _photo_ids.popitem(last=False)


def forget_photo(key):
    return _photo_ids.pop(key,None) is not None


async def media_for(ad,index):
    images = pictures(ad) if ad else []
    chosen = images[index % len(images)] if images else ''
    if chosen and chosen in _photo_ids:
        _photo_ids.move_to_end(chosen)
        return _photo_ids[chosen],chosen,True
    if chosen and _failed_photos.get(chosen,timezone.now()) <= timezone.now():
        try:
            data = await asyncio.to_thread(download_photo,chosen,ad.link)
            return BufferedInputFile(data,filename='car.jpg'),chosen,True
        except Exception as error:
            logger.warning('Result photo unavailable source=%s type=%s',ad.platform,type(error).__name__)
            _failed_photos[chosen] = timezone.now()+timedelta(seconds=60)
            while len(_failed_photos)>512: _failed_photos.popitem(last=False)
    return BufferedInputFile(placeholder_photo(),filename='photo-unavailable.jpg'),'placeholder',False


async def render_card(message,view,bot=None,*,clear_list=False):
    ids,counts = await view_rows(view)
    ad = await Advertisement.objects.filter(pk=ids[view.position]).afirst() if ids else None
    images = pictures(ad) if ad else []
    pending = await prefetch_photos(ids,view.position) if ids else None
    view.live_state = dict(view.live_state or {})
    view.live_state['gallery_signature'] = [ad.pk,images] if ad else None
    view.live_state['gallery_pending_id'] = pending
    if pending and view.search_job_id:
        view.live_until = max(view.live_until or timezone.now(),timezone.now()+timedelta(minutes=10))
    view.photo_index = view.photo_index % len(images) if images else 0
    media,key,available = await media_for(ad,view.photo_index)
    text = caption(ad,available,view.filters) if ad else '🔎 На выбранной площадке пока нет подходящих объявлений.\nОбновите результаты после сбора или выберите «Все».'
    markup = keyboard(view,counts,len(ids),len(images),await is_following(view))
    bot = bot or message.bot
    async def deliver(photo):
        if view.message_id:
            return await bot.edit_message_media(chat_id=view.chat_id,message_id=view.message_id,
                media=InputMediaPhoto(media=photo,caption=text,parse_mode='HTML'),reply_markup=markup)
        return await message.answer_photo(photo,caption=text,parse_mode='HTML',reply_markup=markup)

    if view.message_id and view.last_photo==key:
        sent = await bot.edit_message_caption(chat_id=view.chat_id,message_id=view.message_id,
            caption=text,parse_mode='HTML',reply_markup=markup)
    else:
        try:
            sent = await deliver(media)
        except TelegramBadRequest as error:
            expired = any(phrase in str(error).lower() for phrase in ('wrong file','file identifier','file_id','file reference'))
            if key not in _photo_ids or not expired: raise
            _photo_ids.pop(key,None)
            media,key,available = await media_for(ad,view.photo_index)
            if ad: text = caption(ad,available,view.filters)
            sent = await deliver(media)
    if not view.message_id: view.message_id = sent.message_id
    if available: remember_photo(key,sent)
    view.last_photo = key
    fields = ['message_id','position','photo_index','last_photo','live_state','live_until']
    if clear_list:
        view.list_message_id = None
        fields.append('list_message_id')
    await view.asave(update_fields=fields)


async def render_list(message,view,bot=None):
    ids,counts = await view_rows(view)
    page_ids = ids[view.list_page*10:view.list_page*10+10]
    rows = await sync_to_async(Advertisement.objects.in_bulk)(page_ids)
    ads = [rows[pk] for pk in page_ids if pk in rows]
    text = listing(view,ads,len(ids))
    markup = keyboard(view,counts,len(ids),following=await is_following(view),list_mode=True)
    if view.list_message_id:
        await (bot or message.bot).edit_message_text(chat_id=view.chat_id,message_id=view.list_message_id,
            text=text,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)
    else:
        sent = await message.answer(text,parse_mode='HTML',reply_markup=markup,disable_web_page_preview=True)
        view.list_message_id = sent.message_id
    await view.asave(update_fields=['list_message_id','list_page'])


async def return_to_card(message, view, bot):
    """Promote the visible list to a photo card; keep bindings until Telegram succeeds."""
    previous_id, previous_photo, previous_list = view.message_id, view.last_photo, view.list_message_id
    view.message_id = message.message_id
    view.last_photo = ''  # The target currently contains text, even if the photo is unchanged.
    try:
        await render_card(message, view, bot, clear_list=True)
    except Exception:
        view.message_id, view.last_photo, view.list_message_id = previous_id, previous_photo, previous_list
        raise
    if previous_id and previous_id != view.message_id:
        try:
            await bot.edit_message_reply_markup(chat_id=view.chat_id, message_id=previous_id, reply_markup=None)
        except (TelegramBadRequest, TelegramRetryAfter):
            logger.info('Previous result controls unavailable')

async def open_browser(message,user_id,progress=None):
    view = await create_view(user_id,message.chat.id)
    if progress: view.message_id = progress.message_id
    await render_card(message,view)
    return view


async def open_query_browser(message,user_id,query,search_job_id):
    view = await create_view(user_id,message.chat.id,query,search_job_id)
    await render_card(message,view)
    return view


@router.callback_query(F.data.startswith('rv:'))
async def browser_callback(callback,state: FSMContext):
    try:
        _,token,action,*args = callback.data.split(':')
        view = await ResultView.objects.filter(pk=token,user_id=callback.from_user.id,chat_id=callback.message.chat.id,
            created_at__gte=timezone.now()-timedelta(days=7)).afirst()
    except (ValueError,AttributeError,ValidationError):
        view = None
    if not view or callback.message.message_id not in (view.message_id,view.list_message_id):
        await callback.answer('Эта выдача недоступна. Откройте /results.',show_alert=True)
        return
    await callback.answer()
    if action=='noop': return
    if action=='new':
        from bot.handlers.text_search import open_filters
        await open_filters(callback.message,state,callback.from_user.id)
        return
    async with lock_for(token):
        await view.arefresh_from_db()
        list_mode = callback.message.message_id==view.list_message_id
        ids,_ = await view_rows(view)
        try:
            if action=='move' and ids:
                view.position=(view.position+int(args[0]))%len(ids)
                view.photo_index=0
            elif action=='source' and args[0] in ('all','avito','autoru','drom'):
                view.platform=args[0];view.position=view.photo_index=view.list_page=0
            elif action=='sort' and args[0] in SORT_LABELS:
                view.sort_order=args[0]
                ads = await sync_to_async(results)(view.filters,view.sort_order)
                ads,_ = result_order(ads,['avito','autoru','drom'],view.sort_order)
                from bot.live_results import merge_results
                await merge_results(view,[(ad.pk,ad.platform) for ad in ads])
                view.position=view.photo_index=view.list_page=0
            elif action=='refresh':
                view.search_job = await sync_to_async(enqueue)(view.filters)
                view.collect_until = timezone.now()+timedelta(minutes=10)
                view.live_until = timezone.now()+timedelta(hours=1)
                ads = await sync_to_async(results)(view.filters,view.sort_order)
                ads,_ = result_order(ads,['avito','autoru','drom'],view.sort_order)
                from bot.live_results import merge_results
                await merge_results(view,[(ad.pk,ad.platform) for ad in ads])
                view.position=view.photo_index=view.list_page=0
            elif action=='listpage': view.list_page=max(0,int(args[0]))
            elif action=='list': list_mode=True
            elif action=='card':
                if list_mode:
                    view.position = view.list_page*10
                    view.photo_index = 0
                    await return_to_card(callback.message, view, callback.bot)
                    return
                list_mode=False
            elif action=='follow':
                from bot.handlers.tracking import offer_view
                await offer_view(callback.message,view)
                return
            elif action in ('photo','gallery') and ids:
                ad = await Advertisement.objects.filter(pk=ids[view.position]).afirst()
                if not ad: return
                images = pictures(ad)
                if len(images)<=1:
                    await callback.answer(await request_photos(ad),show_alert=True)
                else:
                    if action=='gallery': await request_photos(ad)
                    view.photo_index=(view.photo_index+(int(args[0]) if action=='photo' else 1))%len(images)
            elif action=='status':
                from ads.search_status import current_status_text
                from html import escape
                job = await sync_to_async(enqueue)(view.filters,refresh=False)
                await callback.message.answer(escape(await sync_to_async(current_status_text)(job)))
                return
            else: return
            await view.asave(update_fields=['platform','sort_order','result_ids','position','photo_index','list_page','search_job','collect_until','live_until','live_state'])
            if list_mode: await render_list(callback.message,view,callback.bot)
            else: await render_card(callback.message,view,callback.bot)
        except TelegramBadRequest as error:
            if 'message is not modified' not in str(error):
                logger.warning('Result edit rejected type=%s',type(error).__name__)
                await callback.answer('Не удалось обновить карточку. Откройте /results.',show_alert=True)
        except TelegramRetryAfter as error:
            await callback.answer(f'Повторите через {error.retry_after} с.',show_alert=True)
        except (ValueError,IndexError):
            await callback.answer('Недоступная кнопка. Обновите выдачу.',show_alert=True)