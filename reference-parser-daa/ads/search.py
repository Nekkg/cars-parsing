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
    return re.sub(r'[^\w]+', '', str(value).casefold().replace('+','plus')).replace('_', '')


def snapshot(user_filters):
    fields = ('platform', 'price', 'brand', 'variant', 'engine', 'mileage', 'gearbox', 'owners',
              'condition', 'seller', 'city', 'year', 'body_type', 'color', 'drive', 'steering','radius_km','location','generation_hint')
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
    if user_filters.generation_keys and data.get('vehicle', {}).get('model'):
        from .models import CatalogNode
        model_key = data['vehicle']['model'].get('key')
        nodes = CatalogNode.objects.filter(kind='generation', parent__key=model_key,
                                          key__in=user_filters.generation_keys)
        nodes = sorted(nodes, key=lambda node: (node.data.get('year_from') or 0, node.key))
        data['vehicle'].pop('generation', None)
        data['vehicle']['generations'] = [
            {'name': node.name, 'slug': node.slug, 'key': node.key, **node.data} for node in nodes]
        # Keep missing identities restrictive, rather than silently broadening an old selection.
        data['generation_keys'] = sorted(set(user_filters.generation_keys))
    if data.get('generation_hint') and data.get('vehicle',{}).get('model'):
        from ads.models import CatalogNode
        from parsers.generations import contains_code
        model_key=data['vehicle']['model'].get('key')
        nodes=list(CatalogNode.objects.filter(kind='generation',parent__key=model_key))
        direct=[node for node in nodes if any(contains_code(data['generation_hint'],code)
                for code in (*node.data.get('codes',[]),*node.data.get('frame_codes',[])))]
        families={node.data['generation_number'] for node in direct if node.data.get('generation_number') is not None}
        data['generation_keys']=[node.key for node in nodes if node in direct or node.data.get('generation_number') in families]
        from .vehicle_selection import selected_generations
        if not selected_generations(data):
            selected=sorted((node for node in nodes if node.key in data['generation_keys']),
                key=lambda node:(node.data.get('year_from') or 0,node.key))
            data['generation_years']=[{'name':node.name,'slug':node.slug,'key':node.key,**node.data} for node in selected]
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


def matches_generation(ad, generation):
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
    return True


def matches(ad, filters):
    """Unknown values never satisfy an explicitly requested numeric/model constraint."""
    vehicle = filters.get('vehicle', {})
    if filters.get('radius_km'):
        from .geography import within_radius
        if not within_radius(ad.get('city',''),filters.get('location',{}),filters['radius_km']): return False
    if filters.get('variant') and canonical(filters['variant']) not in canonical(ad.get('name','')):
        return False
    model_node=vehicle.get('model')
    identified=(ad.get('generation_evidence') or {}).get('model_key')
    if model_node and model_node.get('key') and identified and identified!=model_node['key']:
        return False
    if model_node:
        # Old rows lack catalogue identity. Preserve numeric-name punctuation.
        import re
        actual=str(ad.get('model','')).replace(' ','')
        selected=str(model_node.get('slug','')).replace(' ','')
        if re.fullmatch(r'\d+(?:-\d+)*',actual) and re.fullmatch(r'\d+(?:-\d+)*',selected) and actual!=selected:
            return False
    for field, kind in [('brand', 'make'), ('model', 'model')]:
        node = vehicle.get(kind)
        if node and canonical(ad.get(field, '')) not in {canonical(node['name']), canonical(node['slug'])}:
            return False
    if filters.get('generation_hint') and ad.get('generation_key') not in filters.get('generation_keys',[]): return False
    from .vehicle_selection import selected_generations
    generations = selected_generations(filters)
    if generations and not any(matches_generation(ad, generation) for generation in generations):
        return False
    if filters.get('generation_keys') and ad.get('generation_key') not in filters['generation_keys']:
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
        if not values or ('make' in vehicle and field == 'brand') or field=='city' and filters.get('radius_km'):
            continue
        values = values if isinstance(values, list) else [values]
        if 'unknown' in values:
            continue
        if field in ('engine','gearbox','body_type','color','drive','steering','condition','seller'):
            from .filter_schema import matches_attribute
            if not matches_attribute(field, ad.get(field, ''), values): return False
        else:
            actual = canonical(ad.get(field, ''))
            if not actual or actual == 'unknown' or not any(canonical(v) in actual for v in values):
                return False
    return True


SORT_LABELS = {'newest':'Сначала новые объявления','cheap':'Сначала дешевле','expensive':'Сначала дороже'}
SORT_FIELDS = {'newest':('-fresh_at','-created_at','-id'), 'cheap':('price','-created_at','-id'), 'expensive':('-price','-created_at','-id')}


def results(filters, sort_order='newest',defer_media=False,created_since=None,candidate_filter=None):
    from django.db.models.functions import Coalesce
    rows = Advertisement.objects.annotate(fresh_at=Coalesce('published_at', 'created_at')).order_by(*SORT_FIELDS.get(sort_order,SORT_FIELDS['newest']))
    if created_since is not None: rows=rows.filter(created_at__gte=created_since)
    if candidate_filter is not None: rows=rows.filter(candidate_filter)
    if defer_media: rows=rows.defer('images')
    from django.db.models import Q
    vehicle = filters.get('vehicle', {})
    for field, kind in (('brand', 'make'), ('model', 'model')):
        node = vehicle.get(kind)
        if node:
            keys = {canonical(node.get('name', '')), canonical(node.get('slug', ''))} - {''}
            rows = rows.filter(**{field + '_key__in': keys})
    from .vehicle_selection import selected_generations
    keys = [row['key'] for row in selected_generations(filters) if row.get('key')]
    if keys:
        rows = rows.filter(generation_key__in=keys)
    if filters.get('generation_keys') or filters.get('generation_hint'):
        rows = rows.filter(generation_key__in=filters.get('generation_keys', []))
    cities = filters.get('city')
    if filters.get('radius_km') and filters.get('location'):
        from .geography import nearby_names
        location=filters['location']
        names=nearby_names(location['latitude'],location['longitude'],filters['radius_km'])
        rows=rows.filter(city__iregex='|'.join(re.escape(name) for name in names))
    elif cities and 'unknown' not in cities:
        query = Q()
        for city in cities: query |= Q(city__icontains=city.strip())
        rows = rows.filter(query)
    try:
        lo,hi = bounds(filters.get('price'))
        if lo is not None: rows = rows.filter(price__gte=lo)
        if hi is not None: rows = rows.filter(price__lte=hi)
    except ValueError:
        return []
    # Push numeric constraints into SQL before the exact shared matcher. CASE
    # protects casts from unknown/oversized source strings; missing values stay out.
    from django.db.models import Case, When, Value, IntegerField, F
    from django.db.models.functions import Cast
    for field in ('year', 'mileage', 'owners'):
        try:
            lower, upper = bounds(filters.get(field))
        except ValueError:
            return []
        if lower is None and upper is None:
            continue
        column = field
        if field in ('year', 'owners'):
            column = 'numeric_'+field
            rows = rows.annotate(**{column:Case(
                When(**{field+'__regex':r'^[0-9]{1,9}$'}, then=Cast(F(field), IntegerField())),
                default=Value(None), output_field=IntegerField())})
        if lower is not None: rows = rows.filter(**{column+'__gte': lower})
        if upper is not None: rows = rows.filter(**{column+'__lte': upper})
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
