"""Persistent source sessions and shared, bounded collection for active searches."""
import os
import time
import threading
import logging
import json
from core.paths import DIAGNOSTICS_DIR
from urllib.request import Request
from datetime import timedelta
os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings')
import django
django.setup()
from django.db import close_old_connections, transaction, connection
from django.utils import timezone
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from ads.models import Advertisement, CatalogNode, SearchJob
from users.models import UserFilters, User
from parsers.models import Parser
from parsers.sources import source_url, extract_listings, enrich, SourceBlocked, SourceTemporaryError
from core.network import source_opener, proxy_setting
from parsers.browser_session import RecoveringBrowser, session_expired
from parsers.avito_client import AvitoClient, avito_proxy, http_enabled
from parsers.fetch_cache import cached_fetch, FetchPending, cleanup_cache
from parsers.generations import normalize_generation, GenerationIndex
from parsers.date_values import source_datetime


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger('collectors')
PAGES = max(1,min(int(os.getenv('SEARCH_MAX_PAGES','500')),1000))
INTERVAL = max(60,int(os.getenv('SEARCH_INTERVAL_SECONDS','300')))


def saved_backoff_seconds(source):
    from django.utils.dateparse import parse_datetime
    latest=timezone.now()
    from django.db.models import Max
    from parsers.models import VehicleAudit
    audits = VehicleAudit.objects.filter(source=source)
    if connection.features.supports_json_field_contains:
        audit_pause = audits.exclude(evidence__contains={'error_type':'SourceUnavailable'}).aggregate(until=Max('pause_until'))['until']
    else:
        audit_pause = max((row.pause_until for row in audits if row.pause_until and row.evidence.get('error_type') != 'SourceUnavailable'), default=None)
    if audit_pause:latest=max(latest,audit_pause)
    for states in SearchJob.objects.values_list('source_state',flat=True):
        state = states.get(source, {})
        if state.get('error_code') == 'network':
            continue  # A per-search network retry must not pause the entire source.
        value=state.get('retry_after')
        if value:
            parsed=parse_datetime(value)
            if parsed and timezone.is_aware(parsed): latest=max(latest,parsed)
    return max(0,(latest-timezone.now()).total_seconds())


def collection_error_state(error, token, previous=None):
    from urllib.error import HTTPError
    previous = previous or {}
    blocked = isinstance(error, SourceBlocked) or isinstance(error, HTTPError) and error.code in (403, 429)
    transient = isinstance(error, SourceTemporaryError)
    failures = 0
    if transient:
        failures = (previous.get('network_failures', 0) if previous.get('attempt_at') == token else 0) + 1
    retry = transient and failures <= 3
    seconds = 0
    if blocked:
        seconds = 900
    elif retry:
        seconds = min(120, 30 * 2 ** min(failures - 1, 2))
    return dict(status='доступ ограничен' if blocked else 'повтор запроса' if retry else 'ошибка',
                error='', error_code='blocked' if blocked else 'filters' if isinstance(error, ValueError) else 'markup' if isinstance(error, RuntimeError) else 'network',
                retry_after=(timezone.now() + timedelta(seconds=seconds)).isoformat() if seconds else None,
                attempt_at=token, requested_at=None if blocked or retry else token,
                network_failures=failures)

def update_status(job_id, source, **state):
    with transaction.atomic():
        job = SearchJob.objects.select_for_update().get(pk=job_id)
        job.source_state[source] = {**job.source_state.get(source,{}), **state, 'updated_at':timezone.now().isoformat()}
        job.save(update_fields=['source_state'])


def save_ad(ad):
    if not ad.get('price') or not ad.get('name'):
        return False
    key = f"{ad['platform']}:{ad['external_id']}"
    existing = Advertisement.objects.filter(ad_id=key).first() or Advertisement.objects.filter(platform=ad['platform'],link=ad['link']).first()
    fields = {field.name for field in Advertisement._meta.fields} - {'id','ad_id','created_at','published_at'}
    values = {key:value for key,value in ad.items() if key in fields and value is not None}
    for field in Advertisement._meta.fields:
        if field.name in values and field.max_length and isinstance(values[field.name],str):
            values[field.name] = values[field.name][:field.max_length]
    published = source_datetime(ad.get('published_at'))
    if published:
        values['published_at'] = published
    values['last_seen'] = timezone.now()
    if existing:
        generation_fields = {'generation','generation_codes','generation_key','generation_evidence'}
        evidence = values.get('generation_evidence',{})
        identified=evidence.get('model_key')
        same_model=not identified or existing.generation_key.startswith(identified+'/')
        if existing.generation_key and same_model and not evidence.get('label') and not evidence.get('url'):
            for field in generation_fields:
                values.pop(field,None)
            if identified:
                values['generation_evidence']={**existing.generation_evidence,'model_key':identified}
        for name,value in values.items():
            if value != '' and value != [] or name in generation_fields and evidence:
                setattr(existing,name,value)
        existing.save()
    else:
        Advertisement.objects.create(ad_id=key, **values)
    return True


def browser(source):
    if source == 'avito' and http_enabled():
        return AvitoClient()
    session_file = DIAGNOSTICS_DIR / f'session_{source}.json'
    route = avito_proxy() if source == 'avito' else proxy_setting('SOURCES_PROXY_URL')
    saved = json.loads(session_file.read_text()) if session_file.exists() else {}
    endpoint = os.getenv('SELENIUM_URL', '').strip()
    if endpoint and saved.get('id') and saved.get('proxy') == route and saved.get('endpoint') == endpoint:
        session_id = saved['id']
        class AttachedBrowser(webdriver.Remote):
            def start_session(self, capabilities):
                self.session_id = session_id
                self.caps = {}
        try:
            attached = AttachedBrowser(endpoint, options=webdriver.ChromeOptions())
            attached.title
            return attached
        except Exception:
            pass
    options = webdriver.ChromeOptions()
    options.page_load_strategy = 'eager'
    if source == 'avito':
        from core.paths import SESSIONS_DIR
        profile = '/home/seluser/avito-profile' if endpoint else str(SESSIONS_DIR / 'avito-chrome')
        options.add_argument(f'--user-data-dir={profile}')
    options.add_argument(f'--proxy-server={route}' if route else '--no-proxy-server')
    # Avito remains visible in noVNC for a user's manual access check; no CAPTCHA solver.
    if source != 'avito':
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1440,1000')
    driver = webdriver.Remote(endpoint, options=options) if endpoint else webdriver.Chrome(options=options)
    driver.set_page_load_timeout(45)
    session_file.parent.mkdir(exist_ok=True)
    if endpoint:
        session_file.write_text(json.dumps({'id':driver.session_id,'proxy':route,'endpoint':endpoint}))
    return driver


def load_page(driver,source,url):
    if source == 'drom':
        with source_opener().open(Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=25) as response:
            return response.read().decode(response.headers.get_content_charset() or 'windows-1251')
    try:
        driver.get(url)
    except TimeoutException:
        driver.execute_script('window.stop()')
    return driver.page_source


def collection_filters(source, filters):
    if filters.get('radius_km') and source != 'avito':
        filters = {**filters,'city':[]}
    if source != 'avito':
        return filters
    # Shared streams for a city/make/model/year window; other constraints stay local.
    vehicle = {k: v for k, v in filters.get('vehicle', {}).items() if k in ('make', 'model')}
    shared = {'city': filters.get('city', []), 'vehicle': vehicle}
    if filters.get('radius_km'):
        shared['radius_km'] = filters['radius_km']
    from ads.vehicle_selection import effective_year_bounds
    start, end = effective_year_bounds(filters)
    if start is not None or end is not None:
        shared['year'] = f"{start if start is not None else ''}-{end if end is not None else ''}"
    if not vehicle.get('model') and filters.get('variant'):
        shared['variant'] = filters['variant']
    return shared


def listing_html(driver,source,url):
    html=load_page(driver,source,url)
    if source=='autoru' or (source=='avito' and not http_enabled()):
        try:
            WebDriverWait(driver,8).until(lambda d:d.find_elements(By.CSS_SELECTOR,
                '[data-marker="item"], .ListingItem, [data-seo="listing-item"]'))
        except TimeoutException:
            pass
        html=driver.page_source
    return html


def fetch_listing_page(driver, source, url, page, *, generations=None):
    if generations is None:
        generations = GenerationIndex.from_db()
    def load():
        html = listing_html(driver, source, url)
        ads = extract_listings(html, source)
        for ad in ads:
            normalize_generation(ad, generations)
        from bs4 import BeautifulSoup
        from urllib.parse import urlparse, parse_qs
        has_next = False
        for link in BeautifulSoup(html, 'html.parser').select('a[href]'):
            href = link.get('href', '')
            query = parse_qs(urlparse(href).query)
            if 'neighborhood' in query:
                continue
            if f'/page{page+1}/' in href or query.get('page') == [str(page+1)] or query.get('p') == [str(page+1)]:
                has_next = True
                break
        if source != 'drom' and ads:
            has_next = True
        keys = [f"{source}:{ad['external_id']}" for ad in ads]
        from django.db.models import Q
        existing = list(Advertisement.objects.filter(platform=source).filter(
            Q(ad_id__in=keys)|Q(link__in=[ad['link'] for ad in ads])).values_list('ad_id','link'))
        known, known_links = {row[0] for row in existing}, {row[1] for row in existing}
        new_count = sum(f"{source}:{ad['external_id']}" not in known and ad['link'] not in known_links for ad in ads)
        saved = sum(bool(save_ad(ad)) for ad in ads)
        return {'ads': ads, 'has_next': has_next, 'saved_count': saved, 'new_count': new_count}
    payload, reused = cached_fetch(source, 'listing', url, load, ttl=60)
    if reused:
        for ad in payload['ads']:
            before = (ad.get('generation'),ad.get('generation_key'),ad.get('generation_evidence'),ad.get('brand'),ad.get('model'))
            normalize_generation(ad, generations)
            if before != (ad.get('generation'),ad.get('generation_key'),ad.get('generation_evidence'),ad.get('brand'),ad.get('model')):
                save_ad(ad)
    return payload, reused


_DETAIL_FIELDS = ('brand', 'model', 'city', 'year', 'generation', 'generation_codes',
                  'generation_key', 'generation_evidence', 'mileage', 'engine', 'gearbox',
                  'owners', 'condition', 'seller', 'body_type', 'color', 'drive', 'steering',
                  'image', 'images', 'source_modification')
_AVITO_DETAIL_BACKLOG = 150


def fetch_ad_details(driver, source, ad, generations):
    def load():
        enrich(ad, load_page(driver, source, ad['link']), generations)
        save_ad(ad)
        return {'details': {field: ad[field] for field in _DETAIL_FIELDS if ad.get(field) not in (None, '', [])}, 'gallery_checked': True}
    payload, reused = cached_fetch(source, 'detail', ad['link'], load, ttl=3600)
    if reused:
        ad.update({field: value for field, value in payload['details'].items() if field in _DETAIL_FIELDS})
        normalize_generation(ad, generations)
        # The current list price remains authoritative; never cache a stale price.
        save_ad(ad)
    return ad


def pending_details(ads, filters, generations):
    """Trim the queue locally, including already known nonmatching generations."""
    from ads.search import matches
    keys = [f"{ad['platform']}:{ad['external_id']}" for ad in ads]
    known = {row.ad_id: row for row in Advertisement.objects.filter(ad_id__in=keys)}
    # Hydrate a whole page in one DB read, including previously checked nonmatches.
    from parsers.models import SourceFetch
    from parsers.fetch_cache import fetch_key
    cache_keys = {ad['external_id']: fetch_key('avito', 'detail', ad['link'])
                  for ad in ads if ad['platform'] == 'avito' and ad.get('link')}
    cached = dict(SourceFetch.objects.filter(pk__in=cache_keys.values(), expires_at__gt=timezone.now())
                  .values_list('key', 'payload')) if cache_keys else {}
    basic = {k:v for k,v in filters.items() if k in ('price','city','year','brand','platform','location','radius_km')}
    basic['vehicle'] = {k:v for k,v in filters.get('vehicle',{}).items() if k in ('make','model')}
    from ads.vehicle_selection import selected_generations, effective_year_bounds, in_selected_years
    generation = selected_generations(filters)
    start, end = effective_year_bounds(filters)
    if start is not None and end is not None and start>end:return []
    if start is not None or end is not None:basic['year'] = f"{start or ''}-{end or ''}"
    queue = []
    for ad in ads:
        existing = known.get(f"{ad['platform']}:{ad['external_id']}")
        if existing:
            incoming = ad.get('generation_evidence') or {}
            if not incoming.get('label') and not incoming.get('url') and not ad.get('generation'):
                ad['generation_evidence'] = existing.generation_evidence
            for field in ('generation','generation_codes','generation_key','generation_evidence','image','city','year','engine','gearbox',
                          'owners','condition','seller','body_type','color','drive','steering','mileage'):
                if ad.get(field) in (None, '', []) and getattr(existing, field) not in (None, '', []):
                    ad[field] = getattr(existing, field)
        payload = cached.get(cache_keys.get(ad['external_id']), {}) if ad['platform'] == 'avito' else {}
        details = payload.get('details')
        complete = isinstance(details, dict) and payload.get('gallery_checked') is True
        cache_applied = False
        if isinstance(details, dict):
            values = {key: value for key, value in details.items()
                      if key in _DETAIL_FIELDS and value not in (None, '', [])}
            cache_applied = any(ad.get(key) != value for key, value in values.items())
            ad.update(values)
        before = (ad.get('generation'),ad.get('generation_key'),ad.get('generation_evidence'),ad.get('brand'),ad.get('model'))
        normalize_generation(ad, generations)
        if cache_applied or before != (ad.get('generation'),ad.get('generation_key'),ad.get('generation_evidence'),ad.get('brand'),ad.get('model')):
            save_ad(ad)
        if not in_selected_years(ad.get('year'),filters):continue
        candidate_filters = dict(basic)
        if not str(ad.get('year')).isdigit():candidate_filters.pop('year',None)
        if not ad.get('city'):
            candidate_filters.pop('city',None)
            candidate_filters.pop('radius_km',None)
            candidate_filters.pop('location',None)
        # A source may expose only the Lexus family until its detail page supplies the badge.
        model = candidate_filters.get('vehicle', {}).get('model', {})
        make = candidate_filters.get('vehicle', {}).get('make', {})
        if make.get('slug') == 'lexus' and model:
            from .model_names import lexus_badge
            badge = lexus_badge(model.get('slug', ''))
            if badge and ad.get('model', '').casefold() == badge[0]:
                candidate_filters['vehicle'] = {'make': make}
        # Known contrary attributes cannot be repaired by opening a detail page.
        # Missing attributes are left for enrichment; they do not become matches.
        for field in ('mileage', 'owners', 'engine', 'gearbox', 'body_type', 'color',
                      'drive', 'steering', 'condition', 'seller'):
            if filters.get(field) and ad.get(field) not in (None, '', [], 'unknown'):
                candidate_filters[field] = filters[field]
        if not matches(ad,candidate_filters):
            continue
        if filters.get('generation_hint') and ad.get('generation_key') and ad['generation_key'] not in filters.get('generation_keys',[]): continue
        if generation and ad.get('generation_key'):
            generation_filters = {'vehicle':filters['vehicle'], 'generation_keys': filters.get('generation_keys', [])}
            if not matches(ad,generation_filters):
                continue
        if matches(ad,filters) and (ad['platform'] != 'avito' or ad.get('image')):
            continue
        if complete:
            continue  # This card was checked already; even unresolved details need no repeat within TTL.
        queue.append(ad)
    return queue


def collect(driver, source, job):
    """One page per turn: new requests do not wait behind a whole-country crawl."""
    from parsers.query_filters import variants
    import hashlib
    from ads.generation_catalog import ensure_generation_catalog
    ensure_generation_catalog(job.filters)
    from parsers.query_filters import hydrate_generation_years
    from ads.vehicle_selection import effective_year_bounds
    job.filters=hydrate_generation_years(job.filters)
    start,end=effective_year_bounds(job.filters)
    if start is not None and end is not None and start>end:
        update_status(job.pk,source,status='готово',count=0,pages=0,detail_queue=[],
            listing_finished=True,error='',error_code='',cycle=job.requested_at.isoformat(),
            requested_at=job.requested_at.isoformat(),checked_at=timezone.now().isoformat())
        return
    state = SearchJob.objects.get(pk=job.pk).source_state.get(source, {})
    token = job.requested_at.isoformat()
    resumable_block = state.get('error_code') == 'blocked' and (state.get('detail_queue') or not state.get('listing_finished'))
    if state.get('cycle') != token or state.get('requested_at') == token and not resumable_block:
        state = {'cycle':token, 'route':0, 'next_page':1, 'count':0, 'pages':0, 'fingerprints':[]}
    year_query_changed = (start is not None or end is not None) and (
        state.get('query_years') != [start, end] or
        source == 'avito' and state.get('query_years_version') != 2)
    from parsers.query_filters import city_slug
    native_location = [city_slug(job.filters, 'avito'), job.filters.get('radius_km') or 0]
    geo_query_changed = (source == 'avito' and native_location[0] and native_location[1]
                         and state.get('query_location') != native_location)
    generations = None
    if state.get('detail_queue'):
        generations = GenerationIndex.from_db()
        queue = pending_details(state['detail_queue'], job.filters, generations)
        state['detail_queue'] = queue
        update_status(job.pk,source,detail_queue=queue,requested_at=None)
        prefer_listing = (source == 'avito' and state.get('prefer_listing') and
                          not state.get('listing_finished') and not year_query_changed and not geo_query_changed and
                          len(queue) < _AVITO_DETAIL_BACKLOG)
        # A legacy backlog must not delay the first correctly filtered page.
        if not prefer_listing and not (source == 'avito' and (year_query_changed or geo_query_changed)):
            errors = state.get('detail_errors',0)
            processed = bool(queue)
            for _ in range(min(3,len(queue))):
                ad = queue[0]
                try:
                    fetch_ad_details(driver, source, ad, generations)
                except (SourceBlocked, SourceTemporaryError, FetchPending):
                    raise
                except Exception as error:
                    if session_expired(error): raise
                    errors += 1
                    logger.warning('%s detail: %s',source,type(error).__name__)
                queue.pop(0)
                # Persist after each detail: a later block cannot undo this progress.
                update_status(job.pk,source,detail_queue=queue,detail_errors=errors)
            finished = not queue and state.get('listing_finished') and not year_query_changed
            update_status(job.pk,source,detail_queue=queue,detail_errors=errors,
                error_code='',attempt_at=token,prefer_listing=source == 'avito',network_failures=0,retry_after=None,
                status=('частично' if errors else 'лимит страниц' if state.get('limited') else 'готово') if finished else 'проверка карточек' if queue else 'сбор выдачи',
                requested_at=token if finished else None,
                checked_at=timezone.now().isoformat() if finished else None,
                error=f'Не загрузились карточки: {errors}' if errors else '')
            if finished or processed:
                return
    # A cursor saved before native year filtering must not skip the first pages.
    # Saved ads and unfinished detail work remain valid and are kept above.
    if year_query_changed or geo_query_changed:
        state.update(route=0, next_page=1, count=0, pages=0, fingerprints=[], known_pages=0,
                     listing_finished=False, limited=False, query_years=[start, end], query_years_version=2, query_location=native_location)
    routes = variants(job.filters)
    route = state.get('route',0)
    page = state.get('next_page',1)
    filters = routes[route]
    count = state.get('count',0)
    update_status(job.pk,source,**{**state,'status':'сбор выдачи','error':'','error_code':'','attempt_at':token,'checked_at':None})
    url = source_url(source, collection_filters(source, filters), page)
    if generations is None:
        generations = GenerationIndex.from_db()
    payload, reused = fetch_listing_page(driver, source, url, page, generations=generations)
    ads = payload['ads']
    fingerprint = hashlib.sha256('|'.join(sorted(ad['external_id'] for ad in ads)).encode()).hexdigest()
    repeated = fingerprint in state.get('fingerprints',[])

    errors = state.get('detail_errors',0)
    if not repeated:
        count += payload['saved_count']
        # Persist listing cards immediately; the bot can serve them during enrichment.
        details = pending_details(ads, job.filters, generations)
        if source == 'avito' and year_query_changed:
            # Fresh, correctly filtered cards get priority; retain older unfinished work.
            fresh = {ad['external_id'] for ad in details}
            state['detail_queue'] = details + [ad for ad in state.get('detail_queue', [])
                                               if ad['external_id'] not in fresh]
        else:
            queued = {ad['external_id'] for ad in state.get('detail_queue', [])}
            state.setdefault('detail_queue', []).extend(ad for ad in details if ad['external_id'] not in queued)
        update_status(job.pk,source,count=count,pages=state.get('pages',0)+1,
                      status='проверка карточек' if state['detail_queue'] else 'сбор выдачи')
    state['fingerprints'] = (state.get('fingerprints',[]) + [fingerprint])[-100:]
    has_next = payload['has_next']
    capped = page >= PAGES
    state['known_pages'] = state.get('known_pages',0)+1 if not payload.get('new_count',0) else 0
    caught_up = job.kind == 'tracking' and state['known_pages'] >= 2
    end_route = not ads or repeated or not has_next or capped or caught_up
    if end_route:
        route += 1
        state['fingerprints'] = []
        state['known_pages'] = 0
    listing_finished = route >= len(routes)
    finished = listing_finished and not state.get('detail_queue')
    update_status(job.pk,source,cycle=token,route=route,next_page=1 if end_route else page+1,
        error_code='',attempt_at=token,network_failures=0,retry_after=None,
        count=count,pages=state.get('pages',0)+1,detail_errors=errors,fingerprints=state['fingerprints'],known_pages=state['known_pages'],
        status=('лимит страниц' if capped else 'частично' if errors else 'готово') if finished else 'сбор выдачи',
        error=f'Не загрузились карточки: {errors}' if errors else '',
        checked_at=timezone.now().isoformat() if finished else None,
        requested_at=token if finished else state.get('requested_at'),
        limited=capped or state.get('limited',False), detail_queue=state.get('detail_queue',[]),
        listing_finished=listing_finished, prefer_listing=False)
    logger.info('%s job=%s page=%s saved=%s finished=%s',source,job.pk,page,count,finished)


def active_search_jobs():
    ids = UserFilters.objects.exclude(search_job_id=None).values_list('search_job_id', flat=True)
    from django.db.models import Q
    from ads.models import ResultView, WebSearch
    views = ResultView.objects.filter(collect_until__gt=timezone.now()).values_list('search_job_id',flat=True)
    web_views = WebSearch.objects.filter(collect_until__gt=timezone.now()).values_list('search_job_id',flat=True)
    from users.models import TrackedSearch
    tracked = TrackedSearch.objects.filter(enabled=True,last_check_at__isnull=False).values_list('search_job_id',flat=True)
    legacy=UserFilters.objects.filter(user_id__in=User.objects.filter(monitoring_enabled=True).values('user_id')).values_list('search_job_id',flat=True)
    # A saved form is not a perpetual background subscription. Active cards extend
    # their collection lease; an explicit notification subscription remains active.
    recent=timezone.now()-timedelta(hours=1)
    return SearchJob.objects.filter(Q(pk__in=ids,requested_at__gte=recent)|Q(pk__in=views)|Q(pk__in=web_views)|Q(pk__in=tracked)|Q(pk__in=legacy)).order_by('-requested_at')


def worker(source):
    driver = None
    last_runs = {}
    source_block_until = time.monotonic()+saved_backoff_seconds(source)
    last_cleanup = 0.0
    last_audit = 0.0
    while True:
        try:
            close_old_connections()
            if time.monotonic() - last_cleanup > 600:
                cleanup_cache(source)
                last_cleanup = time.monotonic()
            if not Parser.objects.filter(name=source,is_active=True).exists():
                time.sleep(3)
                continue
            if driver is None and source != 'drom': driver = RecoveringBrowser(lambda: browser(source))
            if source_block_until <= time.monotonic():
                from parsers.media_queue import process_media
                if process_media(driver,source): source_block_until=time.monotonic()+900
            subscribed = set(UserFilters.objects.filter(user_id__in=User.objects.filter(monitoring_enabled=True).values('user_id')).values_list('search_job_id',flat=True))
            worked = False
            from parsers.vehicle_audit import process_audit,urgent_search
            from parsers.models import MediaRequest
            media_pending=MediaRequest.objects.filter(advertisement__platform=source,finished_at__isnull=True,
                next_attempt_at__lte=timezone.now(),attempts__lt=3).exists()
            # Old full-catalogue searches must not starve a requested audit forever.
            # New interactive requests and queued photos always take priority.
            audited=False
            if source_block_until<=time.monotonic() and time.monotonic()-last_audit>=60 and not media_pending and not urgent_search(source):
                if driver:driver.on_recovery=lambda:None
                if process_audit(driver,source):
                    source_block_until=time.monotonic()+900
                    if source=='avito' and http_enabled() and driver:
                        try:driver.quit()
                        except Exception:pass
                        driver=None
                last_audit=time.monotonic();audited=True
            # One page per current user search, newest requests first.
            for job in active_search_jobs():
                if job.filters.get('platform') and source not in job.filters['platform'] and 'unknown' not in job.filters['platform']: continue
                state = job.source_state.get(source,{})
                pending = state.get('requested_at') != job.requested_at.isoformat() or (state.get('error_code') == 'blocked' and (state.get('detail_queue') or not state.get('listing_finished')))
                if not pending and job.pk not in subscribed: continue
                if source_block_until > time.monotonic():
                    if pending:
                        update_status(job.pk,source,status='доступ ограничен',error_code='blocked',attempt_at=job.requested_at.isoformat(),error='',retry_after=(timezone.now()+timedelta(seconds=source_block_until-time.monotonic())).isoformat())
                    continue
                if state.get('error_code') == 'network' and state.get('retry_after') and state.get('attempt_at') == job.requested_at.isoformat():
                    from django.utils.dateparse import parse_datetime
                    retry_at = parse_datetime(state['retry_after'])
                    if retry_at and timezone.is_aware(retry_at) and retry_at > timezone.now():
                        continue
                if not pending and time.monotonic()-last_runs.get(job.pk,0) < INTERVAL: continue
                worked = True
                try:
                    if driver:
                        driver.on_recovery = lambda: update_status(job.pk,source,status='восстановление браузера',error_code='browser_session')
                    collect(driver,source,job)
                except FetchPending:
                    continue
                except Exception as error:
                    from urllib.error import HTTPError
                    blocked = isinstance(error,SourceBlocked) or isinstance(error,HTTPError) and error.code in (403,429)
                    if session_expired(error):
                        update_status(job.pk,source,status='восстановление браузера',error_code='browser_session',error='',requested_at=None)
                        try: driver.quit()
                        except Exception: pass
                        driver = None
                        time.sleep(5)
                        break
                    current = SearchJob.objects.get(pk=job.pk).source_state.get(source, {})
                    update_status(job.pk, source, **collection_error_state(error, job.requested_at.isoformat(), current))
                    logger.warning('%s job=%s %s',source,job.pk,type(error).__name__)
                    if isinstance(error, (SourceBlocked, SourceTemporaryError)):
                        # These messages are controlled by our code, without response bodies/tokens.
                        logger.warning('%s access check: %s', source, str(error))
                    if blocked:
                        source_block_until = time.monotonic()+900
                        if source == 'avito' and http_enabled() and driver:
                            # A rejected session is discarded once; retry only after the cooldown.
                            try: driver.quit()
                            except Exception: pass
                            driver = None
                last_runs[job.pk] = time.monotonic()
                # Re-read newest jobs between pages.
            if not worked and not audited and source_block_until <= time.monotonic():
                from parsers.models import MediaRequest
                if not MediaRequest.objects.filter(advertisement__platform=source,finished_at__isnull=True,
                    next_attempt_at__lte=timezone.now(),attempts__lt=3).exists():
                    from parsers.vehicle_audit import process_audit
                    if driver: driver.on_recovery = lambda: None
                    if process_audit(driver,source):
                        source_block_until=time.monotonic()+900
                        if source=='avito' and http_enabled() and driver:
                            try:driver.quit()
                            except Exception:pass
                            driver=None
                    last_audit=time.monotonic()
            time.sleep(2)
        except Exception as error:
            logger.warning('%s worker: %s',source,type(error).__name__)
            if driver:
                try: driver.quit()
                except Exception: pass
            driver = None
            time.sleep(10)

def main():
    threads = [threading.Thread(target=worker,args=(source,),daemon=False) for source in ('avito','drom','autoru')]
    for thread in threads: thread.start()
    for thread in threads: thread.join()


if __name__ == '__main__':
    main()
