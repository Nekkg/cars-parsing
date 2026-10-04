import hashlib
import json
import os
import re

from django.utils import timezone
from django.db import transaction
from datetime import timedelta
from .catalog import vehicle_path
from .models import SearchJob, Advertisement
from .filter_values import bounds


def canonical(value):
    return re.sub(r'[^\w]+', '', str(value).casefold()).replace('_', '')


def snapshot(user_filters):
    fields = ('platform', 'price', 'brand', 'variant', 'engine', 'mileage', 'gearbox', 'owners',
              'condition', 'seller', 'city', 'year', 'body_type', 'color', 'drive', 'steering')
    data = {field: getattr(user_filters, field) for field in fields}
    if user_filters.vehicle_id:
        path = vehicle_path(user_filters.vehicle)
        model = path.get('model')
        if model and model.data.get('canonical_model_key'):
            from .models import CatalogNode
            target = CatalogNode.objects.filter(kind='model',key=model.data['canonical_model_key']).first()
            if target:
                path['model'] = target
        data['vehicle'] = {kind: {'name': node.name, 'slug': node.slug, 'key': node.key, **node.data} for kind, node in path.items()}
    return data


def request_search(user_filters):
    data = snapshot(user_filters)
    job = enqueue(data)
    user_filters.search_job = job
    user_filters.save(update_fields=['search_job'])
    return job


def enqueue(data, refresh=True):
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    job, created = SearchJob.objects.get_or_create(fingerprint=fingerprint, defaults={'filters': data})
    if refresh and not created:
        from .search_status import selected_sources
        # Joining an in-flight search must not cancel another user's waiter.
        with transaction.atomic():
            job = SearchJob.objects.select_for_update().get(pk=job.pk)
            token = job.requested_at.isoformat()
            pending = any(job.source_state.get(source, {}).get('requested_at') != token
                          for source in selected_sources(data))
            fresh_seconds = max(30, min(int(os.getenv('SEARCH_CACHE_SECONDS', '120')), 600))
            if not pending and job.requested_at <= timezone.now() - timedelta(seconds=fresh_seconds):
                job.requested_at = timezone.now()
                job.save(update_fields=['requested_at'])
    return job


def matches(ad, filters):
    """Unknown values never satisfy an explicitly requested numeric/model constraint."""
    vehicle = filters.get('vehicle', {})
    if filters.get('variant') and canonical(filters['variant']) not in canonical(ad.get('name','')):
        return False
    for field, kind in [('brand', 'make'), ('model', 'model')]:
        node = vehicle.get(kind)
        if node and canonical(ad.get(field, '')) not in {canonical(node['name']), canonical(node['slug'])}:
            return False
    generation = vehicle.get('generation')
    if generation:
        from .generation_labels import facelift
        actual_key = ad.get('generation_key','')
        expected_key = generation.get('key')
        expected_slug = generation.get('slug')
        if expected_key:
            if actual_key != expected_key:
                return False
        elif actual_key and expected_slug:
            if actual_key.rsplit('/',1)[-1] != expected_slug:
                return False
        else:
            if (ad.get('generation_evidence') or {}).get('status') in ('unknown','ambiguous','conflict'):
                return False
            actual_generation = ad.get('generation','')
            description = generation.get('description','')
            if description and actual_generation and facelift(description) != facelift(actual_generation):
                return False
            codes = generation.get('codes',[])
            actual = {canonical(s) for s in ad.get('generation_codes',[])}
            if codes:
                if not actual.intersection(canonical(c) for c in codes):
                    return False
            elif not description or description not in actual_generation:
                return False
        year = ad.get('year')
        if not str(year).isdigit():
            return False
        if generation.get('year_from') and int(year) < generation['year_from']:
            return False
        if generation.get('year_to') and int(year) > generation['year_to']:
            return False
    for field in ('price', 'year', 'mileage', 'owners'):
        value = filters.get(field)
        if not value or value == 'unknown':
            continue
        try:
            lower, upper = bounds(value)
            actual_number = int(ad.get(field))
            if (lower is not None and actual_number < lower) or (upper is not None and actual_number > upper):
                return False
        except (ValueError, TypeError):
            return False
    for field in ('platform', 'brand', 'city', 'engine', 'gearbox', 'body_type', 'color', 'drive', 'steering', 'condition', 'seller'):
        values = filters.get(field)
        if not values or ('make' in vehicle and field == 'brand'):
            continue
        values = values if isinstance(values, list) else [values]
        if 'unknown' in values:
            continue
        actual = canonical(ad.get(field, ''))
        if not actual or actual == 'unknown' or not any(canonical(v) in actual for v in values):
            return False
    return True


SORT_LABELS = {'newest':'Сначала новые объявления','cheap':'Сначала дешевле','expensive':'Сначала дороже'}
SORT_FIELDS = {'newest':('-fresh_at','-created_at','-id'), 'cheap':('price','-created_at','-id'), 'expensive':('-price','-created_at','-id')}


def results(filters, sort_order='newest'):
    from django.db.models.functions import Coalesce
    rows = Advertisement.objects.annotate(fresh_at=Coalesce('published_at', 'created_at')).order_by(*SORT_FIELDS.get(sort_order,SORT_FIELDS['newest']))
    from django.db.models import Q
    vehicle = filters.get('vehicle', {})
    for field, kind in (('brand', 'make'), ('model', 'model')):
        node = vehicle.get(kind)
        if node:
            keys = {canonical(node.get('name', '')), canonical(node.get('slug', ''))} - {''}
            rows = rows.filter(**{field + '_key__in': keys})
    generation = vehicle.get('generation',{})
    if generation.get('key'):
        rows = rows.filter(generation_key=generation['key'])
    cities = filters.get('city')
    if cities and 'unknown' not in cities:
        query = Q()
        for city in cities: query |= Q(city__icontains=city.strip())
        rows = rows.filter(query)
    try:
        lo,hi = bounds(filters.get('price'))
        if lo is not None: rows = rows.filter(price__gte=lo)
        if hi is not None: rows = rows.filter(price__lte=hi)
    except ValueError:
        return []
    platforms = filters.get('platform')
    if platforms and 'unknown' not in platforms:
        rows = rows.filter(platform__in=platforms)
    return [ad for ad in rows.iterator() if matches(ad.__dict__, filters)]


def result_order(ads, sources, sort_order='newest'):
    """Newest first card per selected source, followed by the shared sorted stream."""
    if sort_order != 'newest':
        return ads, min(3, len(ads))
    from .search_status import SOURCE_NAMES
    first = {}
    for ad in ads:
        platform = getattr(ad, 'platform', None)
        if platform in sources and platform not in first:
            first[platform] = ad
    heads = [first[source] for source in SOURCE_NAMES if source in first]
    head_ids = {ad.pk for ad in heads}
    return heads + [ad for ad in ads if ad.pk not in head_ids], len(heads)


def result_page(ads, first_size, page):
    """A missing source shortens only the first batch, without losing tail rows."""
    if page == 0:
        return ads[:first_size]
    start = first_size + (page - 1) * 3
    return ads[start:start + 3]
