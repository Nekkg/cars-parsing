"""Resumable real-ad checks on idle shared source workers; never send test messages."""
from datetime import timedelta
from time import monotonic
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from bs4 import BeautifulSoup
from ads.models import Advertisement
from ads.search import matches
from parsers.models import VehicleAudit,SourceFetch
from parsers.generations import GenerationIndex
from parsers.fetch_cache import cached_fetch,fetch_key,FetchPending
from parsers.sources import source_url,extract_listings,enrich,SourceBlocked,SourceUnavailable
from parsers.photos import unique_photos

_index=None
_index_until=0.0


def index():
    global _index,_index_until
    if _index is None or monotonic()>_index_until:
        _index=GenerationIndex.from_db();_index_until=monotonic()+300
    return _index


def query_for(model):
    if model.data.get('canonical_model_key'):
        from ads.models import CatalogNode
        model=CatalogNode.objects.filter(key=model.data['canonical_model_key'],kind='model',parent_id=model.parent_id).select_related('parent').first() or model
    return {'vehicle':{'make':{'key':model.parent.key,'slug':model.parent.slug,'name':model.parent.name},
        'model':{'key':model.key,'slug':model.slug,'name':model.name}}}


def listing(driver,source,url):
    from parsers.collector import listing_html
    now=timezone.now()
    known=SourceFetch.objects.filter(pk=fetch_key(source,'listing',url),expires_at__gt=now).first()
    if known and known.payload.get('ads') is not None:return known.payload['ads']
    def load():
        ads=extract_listings(listing_html(driver,source,url),source)
        for ad in ads:index().normalize(ad)
        return {'ads':ads}
    return cached_fetch(source,'audit_listing',url,load,ttl=300)[0]['ads']


def card(driver,ad):
    from parsers.collector import load_page
    def load():
        html=load_page(driver,ad['platform'],ad['link'])
        soup=BeautifulSoup(html,'html.parser')
        from parsers.sources import check_block
        check_block(soup,ad['platform'])
        selector={'avito':'[data-marker="item-view/item-params"]',
            'autoru':'.CardInfo, .CardInfoGrouped, [class*="CardInfoRow"], [class*="CardInfoGroupedRow"], [class*="CardInfoSummary"]',
            'drom':'[data-ftid^="specification"]'}[ad['platform']]
        if not soup.find('h1') or not soup.select_one(selector):
            raise ValueError('The requested ad card is not available')
        # A redirect/recommendation is not evidence for the requested listing.
        from urllib.parse import urlparse
        import re
        canonical=soup.find('link',rel='canonical')
        meta=soup.find('meta',attrs={'property':'og:url'})
        actual=canonical.get('href') if canonical else meta.get('content') if meta else None
        if actual:
            found=re.search(r'(\d{6,})(?:-[\w-]+)?(?:\.html)?/?$',urlparse(actual).path)
            if not found or found[1]!=ad['external_id']:
                raise ValueError('The card belongs to a different listing')
        enriched=enrich(dict(ad),html,index())
        return {'ad':enriched}
    return cached_fetch(ad['platform'],'audit_card',ad['link'],load,ttl=300)[0]['ad']


def urgent_search(source):
    from parsers.collector import active_search_jobs
    recent=timezone.now()-timedelta(minutes=1)
    for job in active_search_jobs().filter(requested_at__gte=recent):
        platforms=job.filters.get('platform') or []
        if platforms and source not in platforms and 'unknown' not in platforms:continue
        if job.source_state.get(source,{}).get('requested_at')!=job.requested_at.isoformat():return True
    return False


def verify(driver,model,source):
    from parsers.collector import save_ad
    from bot.photo_delivery import download_photo
    filters=query_for(model)
    url=source_url(source,filters)
    proof={'query_url':url,'model_key':model.key,'scope':'countrywide, make + model, no generation/price/city constraints'}
    ads=listing(driver,source,url)
    proof['listing_count']=len(ads)
    if not ads:return 'empty',proof
    candidates=[]
    for ad in ads:
        if matches(ad,filters):candidates.append(ad)
    # A source may provide only a family name until the detail card identifies the badge.
    if not candidates:
        for ad in ads:
            make=index().identify_make(ad)
            if make and make.key==model.parent.key and index().identify_model(ad) is None:candidates.append(ad)
    if model.parent.slug == 'lexus':
        from .model_names import lexus_candidate_score
        candidates.sort(key=lambda ad:lexus_candidate_score(ad,model.slug),reverse=True)
    proof['candidates']=len(candidates)
    problems=[]
    for candidate in candidates[:3]:
        if urgent_search(source):raise FetchPending()
        try:
            ad=card(driver,candidate)
            if not matches(ad,filters):
                problems.append('model_mismatch');continue
            photos=unique_photos([ad.get('image',''),*(ad.get('images') or [])],source)
            if not photos:
                problems.append('missing_photo');continue
            download_photo(photos[0],ad['link'])
            save_ad(ad)
            row=Advertisement.objects.get(ad_id=source+':'+ad['external_id'])
            if not matches(dict(row.__dict__),filters):
                problems.append('saved_filter_mismatch');continue
            proof.update(ad_id=row.ad_id,ad_pk=row.pk,ad_url=row.link,title=row.name,
                price=row.price,year=row.year,city=row.city,photos=len(photos),photo_checked=True,
                listing_checked=True,card_checked=True,filter_checked=True)
            return 'confirmed',proof
        except (SourceBlocked,FetchPending):raise
        except Exception as error:
            from urllib.error import HTTPError
            if isinstance(error,HTTPError) and error.code in (403,429):raise
            problems.append(type(error).__name__)
    proof.update(problems=problems,examples=[ad.get('name','')[:200] for ad in ads[:3]])
    return ('unverified_card' if candidates else 'no_matching_model'),proof


def process_audit(driver,source):
    """One case per idle turn; source pauses and leases survive worker restarts."""
    now=timezone.now()
    paused=VehicleAudit.objects.filter(source=source).aggregate(until=Max('pause_until'))['until']
    if paused and paused>now:return False
    with transaction.atomic():
        from django.db.models import Q
        row=VehicleAudit.objects.select_for_update(skip_locked=True,of=('self',)).filter(source=source,status='pending').filter(
            Q(lease_until__isnull=True)|Q(lease_until__lte=now)).select_related('model__parent').order_by('pk').first()
        if not row:return False
        row.lease_until=now+timedelta(minutes=8)
        row.attempts+=1
        row.save(update_fields=['lease_until','attempts'])
    blocked=False
    try:
        status,proof=verify(driver,row.model,source)
    except FetchPending:
        row.attempts-=1
        row.lease_until=now+timedelta(seconds=10)
        row.save(update_fields=['attempts','lease_until'])
        return False
    except Exception as error:
        from urllib.error import HTTPError
        blocked=isinstance(error,SourceBlocked) or isinstance(error,HTTPError) and error.code in (403,429)
        status='blocked' if blocked else 'error'
        proof={'query_url':source_url(source,query_for(row.model)),'error_type':type(error).__name__}
        if isinstance(error,(SourceBlocked,SourceUnavailable)):proof['error_reason']=str(error)[:200]
        if blocked:row.pause_until=timezone.now()+timedelta(minutes=15)
        elif isinstance(error,SourceUnavailable):row.pause_until=timezone.now()+timedelta(minutes=1)
    row.status=status;row.evidence=proof;row.checked_at=timezone.now();row.lease_until=None
    row.save(update_fields=['status','evidence','checked_at','lease_until','pause_until'])
    import logging
    logging.getLogger('collectors').info('audit %s %s: %s',source,row.model.key,status)
    export_if_due()
    return blocked
_report_at=0.0
from threading import Lock
_report_lock=Lock()

def export_if_due():
    """Keep the local report current; exports never send Telegram messages."""
    global _report_at
    if monotonic()<_report_at or not _report_lock.acquire(blocking=False):return
    try:
        if monotonic()<_report_at:return
        _report_at=monotonic()+60
        from django.core.management import call_command
        from io import StringIO
        call_command('audit_vehicles',report=True,stdout=StringIO())
    except Exception as error:
        import logging
        logging.getLogger('collectors').warning('Audit report: %s',type(error).__name__)
    finally:_report_lock.release()