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
from django.db import close_old_connections, transaction
from django.utils import timezone
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from ads.models import Advertisement, CatalogNode, SearchJob
from users.models import UserFilters, User
from parsers.models import Parser
from parsers.sources import source_url, extract_listings, enrich, SourceBlocked
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
    for states in SearchJob.objects.values_list('source_state',flat=True):
        value=states.get(source,{}).get('retry_after')
        if value:
            parsed=parse_datetime(value)
            if parsed and timezone.is_aware(parsed): latest=max(latest,parsed)
    return max(0,(latest-timezone.now()).total_seconds())


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
        if existing.generation_key and not evidence.get('label') and not evidence.get('url'):
            for field in generation_fields:
                values.pop(field,None)
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
    if saved.get('id') and saved.get('proxy') == route:
        session_id = saved['id']
        class AttachedBrowser(webdriver.Remote):
            def start_session(self, capabilities):
                self.session_id = session_id
                self.caps = {}
        try:
            attached = AttachedBrowser('http://chrome_driver:4444/wd/hub',options=webdriver.ChromeOptions())
            attached.title
            return attached
        except Exception:
            pass
    options = webdriver.ChromeOptions()
    options.page_load_strategy = 'eager'
    if source == 'avito':
        options.add_argument('--user-data-dir=/home/seluser/avito-profile')
    options.add_argument(f'--proxy-server={route}' if route else '--no-proxy-server')
    # Avito remains visible in noVNC for a user's manual access check; no CAPTCHA solver.
    if source != 'avito':
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1440,1000')
    driver = webdriver.Remote('http://chrome_driver:4444/wd/hub',options=options)
    driver.set_page_load_timeout(45)
    session_file.parent.mkdir(exist_ok=True)
    session_file.write_text(json.dumps({'id':driver.session_id,'proxy':route}))
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
    if source != 'avito':
        return filters
    # One upstream stream for a city/make/model. Personal constraints stay local.
    vehicle = {k: v for k, v in filters.get('vehicle', {}).items() if k in ('make', 'model')}
    shared = {'city': filters.get('city', []), 'vehicle': vehicle}
    if not vehicle.get('model') and filters.get('variant'):
        shared['variant'] = filters['variant']
    return shared


def fetch_listing_page(driver, source, url, page):
    def load():
        html = load_page(driver, source, url)
        if source == 'autoru' or (source == 'avito' and not http_enabled()):
            try:
                WebDriverWait(driver, 8).until(lambda d: d.find_elements(By.CSS_SELECTOR,
                    '[data-marker="item"], .ListingItem, [data-seo="listing-item"]'))
            except TimeoutException:
                pass
            html = driver.page_source
        ads = extract_listings(html, source)
        generations = GenerationIndex.from_db()
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
        saved = sum(bool(save_ad(ad)) for ad in ads)
        return {'ads': ads, 'has_next': has_next, 'saved_count': saved}
    payload, reused = cached_fetch(source, 'listing', url, load)
    if reused:
        generations = GenerationIndex.from_db()
        for ad in payload['ads']:
            before = (ad.get('generation'),ad.get('generation_key'),ad.get('generation_evidence'),ad.get('brand'),ad.get('model'))
            normalize_generation(ad, generations)
            if before != (ad.get('generation'),ad.get('generation_key'),ad.get('generation_evidence'),ad.get('brand'),ad.get('model')):
                save_ad(ad)
    return payload, reused


def fetch_ad_details(driver, source, ad, generations):
    fields = ('brand', 'model', 'city', 'year', 'generation', 'generation_codes',
              'generation_key', 'generation_evidence', 'mileage', 'engine', 'gearbox', 'owners', 'condition', 'seller',
              'body_type', 'color', 'drive', 'steering', 'image')
    def load():
        enrich(ad, load_page(driver, source, ad['link']), generations)
        save_ad(ad)
        return {'details': {field: ad[field] for field in fields if ad.get(field) not in (None, '', [])}}
    payload, reused = cached_fetch(source, 'detail', ad['link'], load, ttl=3600)
    if reused:
        ad.update(payload['details'])
        normalize_generation(ad, generations)
        # The current list price remains authoritative; never cache a stale price.
        save_ad(ad)
    return ad


def pending_details(ads, filters, generations):
    """Trim the queue locally, including already known nonmatching generations."""
    from ads.search import matches
    keys = [f"{ad['platform']}:{ad['external_id']}" for ad in ads]
    known = {row.ad_id: row for row in Advertisement.objects.filter(ad_id__in=keys)}
    basic = {k:v for k,v in filters.items() if k in ('price','city','year','brand','platform')}
    basic['vehicle'] = {k:v for k,v in filters.get('vehicle',{}).items() if k in ('make','model')}
    generation = filters.get('vehicle',{}).get('generation',{})
    if not basic.get('year') and (generation.get('year_from') or generation.get('year_to')):
        basic['year'] = f"{generation.get('year_from') or ''}-{generation.get('year_to') or ''}"
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
        before = (ad.get('generation'),ad.get('generation_key'),ad.get('generation_evidence'),ad.get('brand'),ad.get('model'))
        normalize_generation(ad, generations)
        if before != (ad.get('generation'),ad.get('generation_key'),ad.get('generation_evidence'),ad.get('brand'),ad.get('model')):
            save_ad(ad)
        candidate_filters = dict(basic)
        if not ad.get('city'):
            candidate_filters.pop('city',None)
        if not matches(ad,candidate_filters):
            continue
        if generation and ad.get('generation_key'):
            generation_filters = {'vehicle':filters['vehicle']}
            if not matches(ad,generation_filters):
                continue
        if matches(ad,filters) and (ad['platform'] != 'avito' or ad.get('image')):
            continue
        queue.append(ad)
    return queue


def collect(driver, source, job):
    """One page per turn: new requests do not wait behind a whole-country crawl."""
    from parsers.query_filters import variants
    from ads.search import matches
    import hashlib
    from ads.generation_catalog import ensure_generation_catalog
    ensure_generation_catalog(job.filters)
    state = SearchJob.objects.get(pk=job.pk).source_state.get(source, {})
    token = job.requested_at.isoformat()
    resumable_block = state.get('error_code') == 'blocked' and (state.get('detail_queue') or not state.get('listing_finished'))
    if state.get('cycle') != token or state.get('requested_at') == token and not resumable_block:
        state = {'cycle':token, 'route':0, 'next_page':1, 'count':0, 'pages':0, 'fingerprints':[]}
    if state.get('detail_queue'):
        generations = GenerationIndex.from_db()
        queue = pending_details(state['detail_queue'], job.filters, generations)
        state['detail_queue'] = queue
        errors = state.get('detail_errors',0)
        update_status(job.pk,source,detail_queue=queue,requested_at=None)
        processed = bool(queue)
        for _ in range(min(3,len(queue))):
            ad = queue[0]
            try:
                fetch_ad_details(driver, source, ad, generations)
            except (SourceBlocked, FetchPending):
                raise
            except Exception as error:
                if session_expired(error): raise
                errors += 1
                logger.warning('%s detail: %s',source,type(error).__name__)
            queue.pop(0)
            # Persist after each detail: a later block cannot undo this progress.
            update_status(job.pk,source,detail_queue=queue,detail_errors=errors)
        finished = not queue and state.get('listing_finished')
        update_status(job.pk,source,detail_queue=queue,detail_errors=errors,
            error_code='',attempt_at=token,
            status=('частично' if errors else 'лимит страниц' if state.get('limited') else 'готово') if finished else 'проверка карточек' if queue else 'сбор выдачи',
            requested_at=token if finished else None,
            checked_at=timezone.now().isoformat() if finished else None,
            error=f'Не загрузились карточки: {errors}' if errors else '')
        if finished or processed:
            return
    routes = variants(job.filters)
    route = state.get('route',0)
    page = state.get('next_page',1)
    filters = routes[route]
    count = state.get('count',0)
    update_status(job.pk,source,**{**state,'status':'сбор выдачи','error':'','error_code':'','attempt_at':token,'checked_at':None})
    url = source_url(source, collection_filters(source, filters), page)
    payload, reused = fetch_listing_page(driver, source, url, page)
    ads = payload['ads']
    fingerprint = hashlib.sha256('|'.join(sorted(ad['external_id'] for ad in ads)).encode()).hexdigest()
    repeated = fingerprint in state.get('fingerprints',[])
    generations = GenerationIndex.from_db()
    needs_details = any(job.filters.get(k) for k in ('engine','gearbox','owners','condition','seller','body_type','color','drive','steering')) or bool(job.filters.get('vehicle',{}).get('generation')) or bool(job.filters.get('city') and any(not ad.get('city') for ad in ads))
    errors = state.get('detail_errors',0)
    if not repeated:
        count += payload['saved_count']
        # Persist listing cards immediately; the bot can serve them during enrichment.
        update_status(job.pk,source,count=count,pages=state.get('pages',0)+1,status='проверка карточек' if needs_details else 'сбор выдачи')
        if needs_details:
            state.setdefault('detail_queue', []).extend(pending_details(ads, job.filters, generations))
    state['fingerprints'] = (state.get('fingerprints',[]) + [fingerprint])[-100:]
    has_next = payload['has_next']
    capped = page >= PAGES
    end_route = not ads or repeated or not has_next or capped
    if end_route:
        route += 1
        state['fingerprints'] = []
    listing_finished = route >= len(routes)
    finished = listing_finished and not state.get('detail_queue')
    update_status(job.pk,source,cycle=token,route=route,next_page=1 if end_route else page+1,
        error_code='',attempt_at=token,
        count=count,pages=state.get('pages',0)+1,detail_errors=errors,fingerprints=state['fingerprints'],
        status=('лимит страниц' if capped else 'частично' if errors else 'готово') if finished else 'сбор выдачи',
        error=f'Не загрузились карточки: {errors}' if errors else '',
        checked_at=timezone.now().isoformat() if finished else None,
        requested_at=token if finished else state.get('requested_at'),
        limited=capped or state.get('limited',False), detail_queue=state.get('detail_queue',[]), listing_finished=listing_finished)
    logger.info('%s job=%s page=%s saved=%s finished=%s',source,job.pk,page,count,finished)


def active_search_jobs():
    ids = UserFilters.objects.exclude(search_job_id=None).values_list('search_job_id', flat=True)
    return SearchJob.objects.filter(pk__in=ids).order_by('-requested_at')


def worker(source):
    driver = None
    last_runs = {}
    source_block_until = time.monotonic()+saved_backoff_seconds(source)
    last_cleanup = 0.0
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
            subscribed = set(UserFilters.objects.filter(user_id__in=User.objects.filter(monitoring_enabled=True).values('user_id')).values_list('search_job_id',flat=True))
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
                if not pending and time.monotonic()-last_runs.get(job.pk,0) < INTERVAL: continue
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
                    update_status(job.pk,source,status='доступ ограничен' if blocked else 'ошибка',
                        error='',error_code='blocked' if blocked else 'filters' if isinstance(error,ValueError) else 'markup' if isinstance(error,RuntimeError) else 'network',
                        retry_after=(timezone.now()+timedelta(minutes=15)).isoformat() if blocked else None,
                        attempt_at=job.requested_at.isoformat(),requested_at=None if blocked else job.requested_at.isoformat())
                    logger.warning('%s job=%s %s',source,job.pk,type(error).__name__)
                    if isinstance(error, SourceBlocked):
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
            time.sleep(2)
        except Exception as error:
            logger.warning('%s worker: %s',source,type(error).__name__)
            if driver:
                try: driver.quit()
                except Exception: pass
            driver = None
            time.sleep(10)

if __name__ == '__main__':
    threads = [threading.Thread(target=worker,args=(source,),daemon=False) for source in ('avito','drom','autoru')]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
