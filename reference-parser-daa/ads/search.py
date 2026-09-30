import hashlib
import json
import re

from django.utils import timezone
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
        data['vehicle'] = {kind: {'name': node.name, 'slug': node.slug, **node.data} for kind, node in path.items()}
    return data


def request_search(user_filters):
    data = snapshot(user_filters)
    job = enqueue(data)
    user_filters.search_job = job
    user_filters.save(update_fields=['search_job'])
    return job


def enqueue(data, refresh=True):
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    job, _ = SearchJob.objects.get_or_create(fingerprint=fingerprint, defaults={'filters': data})
    if refresh:
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
        actual_generation = ad.get('generation', '').casefold()
        description = generation.get('description', '').casefold()
        if description and actual_generation and ('рестайл' in description) != ('рестайл' in actual_generation or 'restyl' in actual_generation):
            return False
        codes = generation.get('codes', [])
        actual = {canonical(s) for s in ad.get('generation_codes', [])}
        if codes:
            if not actual.intersection(canonical(c) for c in codes):
                return False
        elif generation.get('description') not in ad.get('generation', ''):
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


SORT_LABELS = {'newest':'Сначала новые загрузки','cheap':'Сначала дешевле','expensive':'Сначала дороже'}
SORT_FIELDS = {'newest':('-created_at','-id'), 'cheap':('price','-created_at','-id'), 'expensive':('-price','-created_at','-id')}


def results(filters, sort_order='newest'):
    rows = Advertisement.objects.order_by(*SORT_FIELDS.get(sort_order,SORT_FIELDS['newest']))
    from django.db.models import Q
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
