"""Database-backed single-flight cache of public normalized source data."""
from datetime import timedelta
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from django.db import transaction
from django.utils import timezone
from parsers.models import SourceFetch
from core.network import proxy_setting
from parsers.avito_client import avito_proxy


class FetchPending(Exception):
    """Another worker already owns this fetch; join it on a later turn."""


def fetch_key(source, kind, url):
    parts = urlsplit(url)
    canonical_url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                               urlencode(sorted(parse_qsl(parts.query))), ''))
    route = avito_proxy() if source == 'avito' else proxy_setting('SOURCES_PROXY_URL')
    # Proxy credentials never enter database rows or logs.
    return hashlib.sha256(f'{source}|{kind}|{route or "direct"}|{canonical_url}'.encode()).hexdigest()


def cached_fetch(source, kind, url, loader, ttl=120):
    key = fetch_key(source, kind, url)
    now = timezone.now()
    with transaction.atomic():
        SourceFetch.objects.get_or_create(key=key, defaults={'source': source, 'expires_at': now})
        row = SourceFetch.objects.select_for_update().get(pk=key)
        if row.payload and row.expires_at > now:
            return row.payload, True
        if row.lease_until and row.lease_until > now:
            raise FetchPending()
        lease = now + timedelta(seconds=180)
        row.lease_until = lease
        row.save(update_fields=['lease_until'])
    try:
        payload = loader()
        if not isinstance(payload, dict) or not payload:
            raise ValueError('Empty source cache payload')
    except Exception:
        SourceFetch.objects.filter(pk=key, lease_until=lease).update(lease_until=None)
        raise
    SourceFetch.objects.filter(pk=key, lease_until=lease).update(
        payload=payload, expires_at=timezone.now() + timedelta(seconds=ttl), lease_until=None)
    return payload, False


def cleanup_cache(source):
    from django.db.models import Q
    now = timezone.now()
    old = SourceFetch.objects.filter(source=source, expires_at__lt=now-timedelta(days=1)).filter(
        Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    ids = list(old.values_list('pk', flat=True)[:200])
    if ids:
        SourceFetch.objects.filter(pk__in=ids).delete()
