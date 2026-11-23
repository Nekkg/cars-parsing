"""Public web serialization and a short shared result cache, without network calls."""
from collections import Counter
from datetime import timedelta
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from .models import Advertisement, WebSearch
from .filter_values import describe
from .search import results, SORT_FIELDS
from .search_status import SOURCE_NAMES, ERROR_MESSAGES, selected_sources
from parsers.photos import unique_photos
from urllib.parse import urlparse


def source_link(ad):
    try:
        parsed = urlparse(ad.link)
        domains = {'avito':'avito.ru', 'autoru':'auto.ru', 'drom':'drom.ru'}
        domain = domains.get(ad.platform, '')
        host = parsed.hostname or ''
        return ad.link if domain and parsed.scheme == 'https' and not parsed.username and (host == domain or host.endswith('.'+domain)) else ''
    except ValueError:
        return ''


def public_ad(ad):
    photos = unique_photos([ad.image, *(ad.images or [])], ad.platform) if ad.platform in SOURCE_NAMES else []
    from .photo_cache import photo_key
    photo_items = [dict(full=reverse('ads:photo', args=[ad.pk, index, 'full']) + '?v=' + photo_key(url, ad.platform, 'full')[:12],
                        card=reverse('ads:photo', args=[ad.pk, index, 'card']) + '?v=' + photo_key(url, ad.platform, 'card')[:12])
                   for index, url in enumerate(photos)]
    known = lambda v: '' if not v or v == 'unknown' else str(v)
    return dict(id=ad.pk, title=ad.name, price=ad.price, year=known(ad.year), city=ad.city,
                mileage=ad.mileage, engine=known(ad.engine), gearbox=known(ad.gearbox),
                drive=known(ad.drive), body_type=known(ad.body_type), color=known(ad.color),
                owners=known(ad.owners), generation=known(ad.generation),
                platform=ad.platform, platform_name=SOURCE_NAMES.get(ad.platform, ad.platform),
                photos=photos, photo_items=photo_items, preview_url=photo_items[0]['card'] if photo_items else '',
                display_photos=[item['full'] for item in photo_items],
                url=source_link(ad), detail_url=reverse('ads:ad_detail', args=[ad.ad_id]))


def stored_snapshot(sort_order='newest', platform='all', page=1):
    """Read the existing public catalogue with SQL pagination, without collection."""
    from django.db.models import Count
    from django.db.models.functions import Coalesce
    rows = Advertisement.objects.filter(platform__in=SOURCE_NAMES, price__gt=0).exclude(name='')
    counts = cache.get('web-feed-counts:v1')
    if counts is None:
        grouped = dict(rows.values('platform').annotate(total=Count('pk')).values_list('platform', 'total'))
        counts = {source:grouped.get(source, 0) for source in SOURCE_NAMES}
        cache.set('web-feed-counts:v1', counts, 3)
    total_all = sum(counts.values())
    total = total_all if platform == 'all' else counts[platform]
    page_size = 18
    pages = max(1, (total+page_size-1)//page_size)
    page = min(max(1, page), pages)
    if platform != 'all': rows = rows.filter(platform=platform)
    rows = rows.annotate(fresh_at=Coalesce('published_at', 'created_at')).order_by(*SORT_FIELDS[sort_order])
    offset = (page-1)*page_size
    items = [public_ad(ad) for ad in rows[offset:offset+page_size]]
    return dict(items=items, total=total, total_all=total_all, counts=counts,
                sources=[dict(id=source, name=name, count=counts[source], status='в базе',
                              collecting=False, retry_seconds=0, message='') for source,name in SOURCE_NAMES.items()],
                collecting=False, page=page, pages=pages, page_size=page_size,
                summary='Последние объявления\nАвтомобили из нашей базы. Укажите условия, чтобы подобрать свою машину.',
                search_id=None, filters={}, mode='feed')


def result_snapshot(search, sort_order='newest', platform='all', page=1):
    job = search.search_job
    # Match once for all sort/platform views. Visible-page photos remain fresh.
    key = f'web-results:v2:{job.fingerprint}'
    cached = cache.get(key)
    if cached is None:
        rows = results(search.filters, defer_media=True)
        cached = {'rows': [(ad.pk, ad.platform, ad.price, ad.created_at.timestamp()) for ad in rows],
                  'counts': dict(Counter(ad.platform for ad in rows))}
        cache.set(key, cached, 3)
    ordered = cached['rows']
    if sort_order in ('cheap','expensive'):
        ordered = sorted(ordered, key=lambda row: (row[2] if sort_order == 'cheap' else -row[2], -row[3], -row[0]))
    ids = [row[0] for row in ordered if platform == 'all' or row[1] == platform]
    page_size = 18
    total = len(ids)
    pages = max(1, (total+page_size-1)//page_size)
    page = min(max(1, page), pages)
    page_ids = ids[(page-1)*page_size:page*page_size]
    ads = Advertisement.objects.in_bulk(page_ids)
    items = [public_ad(ads[pk]) for pk in page_ids if pk in ads]
    counts = {source: cached['counts'].get(source, 0) for source in SOURCE_NAMES}
    sources = []
    active = search.collect_until > timezone.now()
    token = job.requested_at.isoformat()
    for source, name in SOURCE_NAMES.items():
        state = job.source_state.get(source, {})
        chosen = source in selected_sources(search.filters)
        current = state.get('attempt_at') == token or state.get('cycle') == token or state.get('requested_at') == token
        status = state.get('status', 'В очереди') if current else 'В очереди'
        allowed = ('готово','частично','лимит страниц','сбор выдачи','проверка карточек','ошибка','доступ ограничен','повтор запроса','восстановление браузера','В очереди')
        status = status if status in allowed else 'В очереди'
        pending = chosen and state.get('requested_at') != token
        message = ERROR_MESSAGES.get(state.get('error_code'), '') if current and chosen else ''
        if status == 'ошибка' and not message: message = 'Не удалось завершить сбор. Повторите поиск позже.'
        if not chosen: status = 'Не выбран'
        elif pending and not active: status = 'Сбор приостановлен'
        from django.utils.dateparse import parse_datetime
        retry_at = parse_datetime(state.get('retry_after') or '') if current else None
        retry_seconds = max(0, int((retry_at-timezone.now()).total_seconds())) if retry_at and timezone.is_aware(retry_at) else 0
        sources.append(dict(id=source, name=name, status=status, count=counts[source], retry_seconds=retry_seconds,
                            collecting=active and pending, message=message))
    return dict(items=items, total=total, total_all=sum(counts.values()), counts=counts,
                sources=sources, collecting=any(s['collecting'] for s in sources),
                page=page, pages=pages, page_size=page_size,
                summary=describe(search.filters).removeprefix('🚗 '),
                search_id=str(search.pk), filters=search.filters)


def expire_owner_searches(owner):
    # A browser's new search replaces its active request; other sessions remain active.
    WebSearch.objects.filter(owner_key=owner, collect_until__gt=timezone.now()).update(collect_until=timezone.now())
    WebSearch.objects.filter(owner_key=owner, created_at__lt=timezone.now()-timedelta(days=7)).delete()
