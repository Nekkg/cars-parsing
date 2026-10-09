"""Bounded, deduplicated gallery enrichment on the existing source worker."""
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import MediaRequest,SourceFetch
from .fetch_cache import fetch_key,FetchPending
from .sources import SourceBlocked
from .photos import unique_photos


def request_gallery(ad, *, retry=False):
    """All viewers join one request; automatic prefetch never resets exhausted retries."""
    now = timezone.now()
    row, _ = MediaRequest.objects.get_or_create(advertisement=ad, defaults={'next_attempt_at':now})
    if retry and row.finished_at and row.error_code and row.next_attempt_at <= now:
        # A conditional update prevents concurrent viewers resetting a running request.
        MediaRequest.objects.filter(pk=ad.pk, finished_at__isnull=False, next_attempt_at__lte=now).exclude(error_code='').update(
            finished_at=None, attempts=0, error_code='', next_attempt_at=now)
        row.refresh_from_db()
    return row


def process_media(driver,source):
    from parsers.collector import fetch_ad_details
    from parsers.generations import GenerationIndex
    now = timezone.now()
    with transaction.atomic():
        row = MediaRequest.objects.select_for_update(skip_locked=True).filter(advertisement__platform=source,
            finished_at__isnull=True,next_attempt_at__lte=now,attempts__lt=3).select_related('advertisement').order_by('requested_at').first()
        if not row: return False
        row.attempts += 1
        row.next_attempt_at=now+timedelta(seconds=180)
        row.save(update_fields=['attempts','next_attempt_at'])
    ad = row.advertisement
    key = fetch_key(source,'detail',ad.link)
    cached = SourceFetch.objects.filter(pk=key).first()
    cached_images = unique_photos(cached.payload.get('details',{}).get('images') or [],source) if cached else []
    incomplete = not cached_images or len(cached_images) == 1 and not cached.payload.get('gallery_checked')
    if cached and incomplete and (not cached.lease_until or cached.lease_until<=now):
        SourceFetch.objects.filter(pk=key).update(expires_at=now)
    blocked = False
    try:
        details = {**ad.__dict__,'external_id':ad.ad_id.split(':',1)[-1]}
        fetch_ad_details(driver,source,details,GenerationIndex.from_db())
        row.finished_at=timezone.now()
        row.error_code=''
    except FetchPending:
        row.attempts -= 1
        row.next_attempt_at=now+timedelta(seconds=10)
    except Exception as error:
        from urllib.error import HTTPError
        blocked = isinstance(error,SourceBlocked) or isinstance(error,HTTPError) and error.code in (403,429)
        row.error_code='blocked' if blocked else 'failed'
        row.next_attempt_at=now+timedelta(minutes=15 if blocked else 2)
        if row.attempts>=3: row.finished_at=timezone.now()
    row.save(update_fields=['attempts','next_attempt_at','finished_at','error_code'])
    return blocked