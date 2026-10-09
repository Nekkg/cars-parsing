"""A single restartable publisher for shared searches; it never launches collectors."""
import asyncio
import logging
import json
from datetime import timedelta
from collections import defaultdict
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from asgiref.sync import sync_to_async
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from ads.models import ResultView, Advertisement
from ads.search import results, result_order
from ads.search_status import selected_sources
from users.models import UserFilters
from bot.handlers.result_browser import lock_for, view_rows, render_card, render_list
from bot.result_presentation import pictures
from parsers.models import MediaRequest

logger = logging.getLogger(__name__)
INTERVAL = 5
NOTICE_INTERVAL = 30
LABELS = {'avito':'Авито','autoru':'Auto.ru','drom':'Дром'}


def date(value):
    try: parsed=parse_datetime(value) if value else None
    except (ValueError,TypeError): return None
    return parsed if parsed and timezone.is_aware(parsed) else None


def notice_text(source,count):
    remainder=count%100
    noun='объявление' if count%10==1 and remainder!=11 else 'объявления' if count%10 in (2,3,4) and remainder not in (12,13,14) else 'объявлений'
    return f'➕ {LABELS[source]}: {count} {noun} добавлено в ленту'


def finished(job):
    token=job.requested_at.isoformat()
    return all(job.source_state.get(source,{}).get('requested_at')==token for source in selected_sources(job.filters))


@sync_to_async
def candidates():
    now=timezone.now()
    current=UserFilters.objects.filter(user_id=OuterRef('user_id'),search_job_id=OuterRef('search_job_id'))
    rows=ResultView.objects.filter(live_until__gt=now,message_id__isnull=False,search_job__isnull=False).select_related('search_job').defer('result_ids','last_photo').annotate(is_current=Exists(current))
    active=[]
    for view in rows:
        collecting = view.is_current or view.collect_until and view.collect_until>now
        view.media_only = not collecting
        if collecting or (view.live_state or {}).get('gallery_pending_id'):
            active.append(view)
        else:
            ResultView.objects.filter(pk=view.pk).update(live_until=None)
    return active


@sync_to_async
def shared_rows(view):
    ads=results(view.filters,view.sort_order,defer_media=True)
    ads,_=result_order(ads,selected_sources(view.filters),view.sort_order)
    return [(ad.pk,ad.platform) for ad in ads]


async def merge_results(view,rows):
    """Keep the selected car and count only never-before-seen matching IDs."""
    ids=[pk for pk,_ in rows]
    if ids==view.result_ids: return False
    old_ids,_=await view_rows(view)
    selected=old_ids[view.position] if old_ids else None
    state=dict(view.live_state or {})
    seen=set(state.get('seen_ids',view.result_ids))
    added=dict(state.get('added',{}))
    for pk,source in rows:
        if pk not in seen:
            added[source]=added.get(source,0)+1
            seen.add(pk)
    changed=ids!=view.result_ids
    view.result_ids=ids
    displayed=[pk for pk,source in rows if view.platform=='all' or source==view.platform]
    if selected in displayed:
        view.position=displayed.index(selected)
    else:
        view.position=max(0,min(view.position,len(displayed)-1))
        view.photo_index=0
    view.list_page=max(0,min(view.list_page,max(0,(len(displayed)-1)//10)))
    state.update(seen_ids=sorted(seen),added=added)
    if changed:
        state['render_pending']=True
    view.live_state=state
    return changed


async def edit_existing(renderer,view,bot):
    try:
        await renderer(None,view,bot)
        return True
    except TelegramBadRequest as error:
        text=str(error).lower()
        if 'message is not modified' in text: return True
        if any(phrase in text for phrase in ('message to edit not found','message can\'t be edited')):
            return False
        raise


async def publish_notices(bot,view,complete,now):
    state=view.live_state
    notified=dict(state.get('notified',{}))
    messages=dict(state.get('notice_ids',{}))
    timestamps=dict(state.get('notice_times',{}))
    for source,count in state.get('added',{}).items():
        if source not in LABELS or count<=notified.get(source,0): continue
        last=date(timestamps.get(source))
        if not complete and last and now<last+timedelta(seconds=NOTICE_INTERVAL): continue
        text=notice_text(source,count)
        if messages.get(source):
            try:
                await bot.edit_message_text(chat_id=view.chat_id,message_id=messages[source],text=text,parse_mode='HTML')
            except TelegramBadRequest as error:
                if 'message is not modified' not in str(error).lower():
                    if 'message to edit not found' not in str(error).lower(): raise
                    sent=await bot.send_message(view.chat_id,text,disable_notification=True)
                    messages[source]=sent.message_id
        else:
            sent=await bot.send_message(view.chat_id,text,disable_notification=True)
            messages[source]=sent.message_id
        notified[source]=count
        timestamps[source]=now.isoformat()
        state.update(notified=notified,notice_ids=messages,notice_times=timestamps)
        # Persist each delivery separately so a restart does not resend other sources.
        await view.asave(update_fields=['live_state'])
    return all(count<=notified.get(source,0) for source,count in state.get('added',{}).items())


async def publish_view(bot,key,rows,job,now=None,expected_sort=None,*,media_only=False):
    now=now or timezone.now()
    async with lock_for(key):
        view=await ResultView.objects.filter(pk=key,live_until__gt=now,message_id__isnull=False).afirst()
        if not view: return
        if view.search_job_id!=job.pk or expected_sort and view.sort_order!=expected_sort: return
        retry=date((view.live_state or {}).get('retry_at'))
        if retry and retry>now: return
        changed=False if media_only else await merge_results(view,rows)
        progress={source:[value.get('pages'),value.get('count'),len(value.get('detail_queue') or []),value.get('requested_at')] for source,value in job.source_state.items()}
        progress_changed=not media_only and progress!=view.live_state.get('collection_progress')
        if progress_changed:
            view.live_until=max(view.live_until,now+timedelta(hours=1))
        view.live_state['collection_progress']=progress
        if changed or progress_changed:
            await view.asave(update_fields=['result_ids','position','photo_index','list_page','live_state','live_until'])
        gallery_id=view.live_state.get('gallery_pending_id')
        gallery_pending=False
        if gallery_id:
            current=await Advertisement.objects.filter(pk=gallery_id).afirst()
            signature=[current.pk,pictures(current)] if current else None
            gallery_pending=await MediaRequest.objects.filter(pk=gallery_id,finished_at__isnull=True).aexists()
            if signature!=view.live_state.get('gallery_signature'):
                view.live_state['render_pending']=True
            if not gallery_pending:
                view.live_state['gallery_pending_id']=None
            await view.asave(update_fields=['live_state'])
        complete=media_only or finished(job)
        try:
            if view.live_state.get('render_pending'):
                if not await edit_existing(render_card,view,bot):
                    view.live_until=None
                    await view.asave(update_fields=['live_until'])
                    return
                if view.list_message_id and not await edit_existing(render_list,view,bot):
                    view.list_message_id=None
                    await view.asave(update_fields=['list_message_id'])
                view.live_state['render_pending']=False
                await view.asave(update_fields=['live_state'])
            if not await ResultView.objects.filter(pk=view.pk,live_until__gt=timezone.now()).aexists(): return
            delivered=True if media_only else await publish_notices(bot,view,complete,now)
            if complete and delivered and not view.live_state.get('gallery_pending_id'):
                view.live_until=None
                await view.asave(update_fields=['live_until'])
        except TelegramForbiddenError:
            view.live_until=None
            await view.asave(update_fields=['live_until'])
        except TelegramRetryAfter as error:
            view.live_state['retry_at']=(now+timedelta(seconds=error.retry_after+1)).isoformat()
            await view.asave(update_fields=['live_state'])
        except Exception as error:
            logger.warning('Live result delivery failed type=%s',type(error).__name__)
            view.live_state['retry_at']=(now+timedelta(seconds=15)).isoformat()
            await view.asave(update_fields=['live_state'])


async def poll_once(bot):
    # Matching and ordering run once per shared request, not once per user.
    groups=defaultdict(list)
    for view in await candidates():
        if view.media_only:
            # A user may reopen an older card just to browse photos. Do not revive its search.
            await publish_view(bot,view.pk,[],view.search_job,media_only=True)
        else:
            groups[(view.search_job_id,view.sort_order,json.dumps(view.filters,sort_keys=True))].append(view)
    for views in groups.values():
        rows=await shared_rows(views[0])
        job=views[0].search_job
        # Bound Telegram operations, including downloads for previously empty cards.
        for start in range(0,len(views),4):
            await asyncio.gather(*(publish_view(bot,view.pk,rows,job,expected_sort=view.sort_order) for view in views[start:start+4]))


async def live_results_loop(bot):
    logger.info('Live results publisher started')
    while True:
        try:
            await poll_once(bot)
        except Exception as error:
            logger.warning('Live results cycle failed type=%s',type(error).__name__)
        await asyncio.sleep(INTERVAL)