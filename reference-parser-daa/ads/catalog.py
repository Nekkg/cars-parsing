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


def load_children(parent_id=None, force=False):
    parent = CatalogNode.objects.get(pk=parent_id) if parent_id else None
    existing = CatalogNode.objects.filter(parent=parent)
    refreshed = parent.refreshed_at if parent else existing.order_by('-refreshed_at').values_list('refreshed_at', flat=True).first()
    if not force and existing.exists() and refreshed and refreshed > timezone.now() - timedelta(days=7):
        return list(existing)
    if parent and parent.kind == 'generation':
        return []
    url = ('https://www.drom.ru/catalog/' if not parent else parent.data['url'])
    try:
        data = fetch_catalog(url)
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
                years = re.findall(r'\b(?:19|20)\d{2}\b', title)
                codes = title.split(',')[1:-1]
                codes = [s.strip() for s in codes if s.strip()]
                entries.append(('generation', hashlib.sha256(title.encode()).hexdigest()[:16],
                    f"{title} · {group.get('generationInfo', '')}",
                    {'url': url, 'codes': codes, 'description': group.get('generationInfo', ''),
                     'generation_urls': [item['url'] for item in group.get('items', []) if item.get('url')],
                     'year_from': int(years[0]) if years else None,
                     'year_to': int(years[1]) if len(years) > 1 else None}))
        if not entries:
            raise ValueError('В каталоге нет дочерних записей для этого автомобиля.')
        with transaction.atomic():
            for kind, slug, name, metadata in entries:
                key = f'{parent.key}/{slug}' if parent else slug
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
    return list(existing)


def vehicle_path(node):
    result = {}
    while node:
        result[node.kind] = node
        node = node.parent
    return result
