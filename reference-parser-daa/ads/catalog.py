"""Lazy, cached hierarchy from public Drom catalogue pages, not a hand-written list."""
import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.db import transaction
from django.utils import timezone
from .models import CatalogNode
from core.network import source_opener


def fetch_catalog(url):
    with source_opener().open(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=25) as response:
        html = response.read().decode(response.headers.get_content_charset() or 'windows-1251')
    for attrs, body in re.findall(r'<script([^>]*)>(.*?)</script>', html, re.S):
        if 'application/json' in attrs and 'data-drom-module="catalog-' in attrs:
            return json.loads(body)
    raise ValueError('Каталог недоступен или изменилась разметка. Повторите позже.')


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def load_children(parent_id=None, force=False, _visited=()):
    parent = CatalogNode.objects.get(pk=parent_id) if parent_id else None
    if parent and parent.pk in _visited:
        raise ValueError('Circular catalogue alias')
    visited = (*_visited,parent.pk) if parent else _visited
    if parent and parent.kind == 'model' and parent.data.get('canonical_model_key'):
        target = CatalogNode.objects.filter(kind='model',key=parent.data['canonical_model_key'],parent=parent.parent).first()
        if target:
            return load_children(target.pk,force=force,_visited=visited)
    existing = CatalogNode.objects.filter(parent=parent)
    refreshed = parent.refreshed_at if parent else existing.order_by('-refreshed_at').values_list('refreshed_at', flat=True).first()
    if not force and existing.exists() and refreshed and refreshed > timezone.now() - timedelta(days=7):
        return list(existing)
    if parent and parent.kind == 'generation':
        return []
    url = ('https://www.drom.ru/catalog/' if not parent else parent.data['url'])
    try:
        data = fetch_catalog(url)
        twin = data.get('twin') or {}
        twin_url = (twin.get('globalModelLink') or {}).get('url','')
        if parent and parent.kind == 'model' and twin_url and not data.get('generationsInOrder'):
            parsed = urlparse(twin_url)
            match = re.fullmatch(r'/catalog/([^/]+)/([^/]+)/?',parsed.path)
            if parsed.scheme != 'https' or parsed.hostname != 'www.drom.ru' or not match or match[1] != parent.parent.slug:
                raise ValueError('Invalid catalogue alias')
            key = f'{parent.parent.key}/{match[2]}'
            target_name = (twin.get('globalModelName') or {}).get('name',match[2])
            brand_prefix = parent.parent.name+' '
            if target_name.casefold().startswith(brand_prefix.casefold()):
                target_name = target_name[len(brand_prefix):]
            target, _ = CatalogNode.objects.get_or_create(key=key,defaults={'parent':parent.parent,
                'kind':'model','slug':match[2],'name':target_name,
                'data':{'url':twin_url}})
            # Only an explicit catalogue link can join alternative market names.
            parent.data = {**parent.data,'canonical_model_key':key}
            parent.refreshed_at = timezone.now()
            parent.save(update_fields=['data','refreshed_at'])
            return load_children(target.pk,force=force,_visited=visited)
        entries = []
        if not parent:
            for item in walk(data.get('firms', {})):
                match = re.search(r'/catalog/([^/]+)/?$', item.get('url', ''))
                if match and item.get('name'):
                    entries.append(('make', match[1], item['name'], {'url': item['url']}))
        elif parent.kind == 'make':
            for item in data.get('models', []):
                slug = urlparse(item['url']).path.rstrip('/').split('/')[-1]
                entries.append(('model', slug, item['name'], {'url': item['url']}))
        else:
            for group in data.get('generationsInOrder', []):
                title = group['title']
                from .generation_labels import ordinal, facelift
                years = re.findall(r'\b(?:19|20)\d{2}\b', title)
                codes = [s.strip() for s in title.split(',')[1:-1] if s.strip()]
                periods = []
                frames = set()
                for item in group.get('items', []):
                    item_years = re.findall(r'\b(?:19|20)\d{2}\b', item.get('title',''))
                    item_codes = [s for s in re.split(r'[,/;\s]+',item.get('frames','')) if s]
                    frames.update(item_codes)
                    periods.append({'url':item.get('url',''), 'market':(item.get('outlet') or {}).get('name',''),
                        'year_from':int(item_years[0]) if item_years else None,
                        'year_to':int(item_years[1]) if len(item_years)>1 else None,
                        'frame_codes':item_codes})
                description = group.get('generationInfo','')
                entries.append(('generation', hashlib.sha256(title.encode()).hexdigest()[:16],
                    f"{title} · {description}",
                    {'schema_version':2,'url':url, 'codes':codes, 'description':description,
                     'generation_number':ordinal(description),'facelift_number':facelift(description),
                     'frame_codes':sorted(frames), 'periods':periods,
                     'generation_urls':[item['url'] for item in group.get('items',[]) if item.get('url')],
                     'year_from':int(years[0]) if years else None,
                     'year_to':int(years[1]) if len(years)>1 else None}))
        if not entries:
            raise ValueError('В каталоге нет дочерних записей для этого автомобиля.')
        previous_generations = list(existing) if parent and parent.kind == 'model' else []
        previous_models = {node.key:node.data for node in existing} if parent and parent.kind == 'make' else {}
        with transaction.atomic():
            for kind, slug, name, metadata in entries:
                if kind == 'generation':
                    urls = set(metadata.get('generation_urls',[]))
                    same = [node for node in previous_generations if urls.intersection(node.data.get('generation_urls',[]))]
                    # A changed display title/production end must not invalidate saved selections.
                    if len(same) == 1:
                        slug = same[0].slug
                key = f'{parent.key}/{slug}' if parent else slug
                if kind == 'model':
                    old_data = previous_models.get(key,{})
                    if old_data.get('canonical_model_key'):
                        metadata['canonical_model_key'] = old_data['canonical_model_key']
                CatalogNode.objects.update_or_create(key=key, defaults={
                    'parent': parent, 'kind': kind, 'slug': slug, 'name': name[:250],
                    'data': metadata, 'refreshed_at': timezone.now()})
            if parent:
                parent.refreshed_at = timezone.now()
                parent.save(update_fields=['refreshed_at'])
    except Exception:
        if existing.exists():
            return list(existing)
        raise
    return list(existing.all())


def vehicle_path(node):
    result = {}
    while node:
        result[node.kind] = node
        node = node.parent
    return result
