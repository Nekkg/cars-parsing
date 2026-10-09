"""Collect only the current car's gallery, never recommendation/banner images."""
import html
import json
import re
from urllib.parse import urlparse, urljoin

DOMAINS = {'avito': ('avito.st', 'avito.ru'), 'drom': ('drom.ru',),
           'autoru': ('auto.ru', 'yandex.net', 'yandex.ru', 'avatars.avto.ru')}


def valid_photo(value, source):
    if not isinstance(value, str) or not value.strip():
        return ''
    try:
        value = urljoin('https://' + {'avito': 'www.avito.ru', 'drom': 'auto.drom.ru', 'autoru': 'auto.ru'}[source], value.strip())
        parsed = urlparse(value)
    except ValueError:
        return ''
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or parsed.username is not None or len(value) > 500:
        return ''
    # An absent ImageObject.url must never become the site's HTML home page.
    if parsed.path in ('', '/') or re.search(r'\.(?:svg|gif|html?)(?:[?#]|$)', value, re.I):
        return ''
    if not any(host == domain or host.endswith('.' + domain) for domain in DOMAINS[source]):
        return ''
    return value


def unique_photos(values, source):
    result = {}
    for value in values:
        value = valid_photo(value, source)
        if not value:
            continue
        path = urlparse(value).path
        # One asset can appear in the gallery and JSON-LD at several sizes.
        match = re.fullmatch(r'(/photo/v2/[^/]+)/gen(\d+)\.(?:jpe?g|png|webp)', path) if source == 'drom' else None
        key = match[1] if match else value
        size = int(match[2]) if match else 0
        if source == 'autoru':
            match = re.fullmatch(r'(/get-autoru(?:-[\w-]+)?/[^/]+/[^/]+)/(?:([0-9]+)x([0-9]+)|small)', path)
            if match:
                key = match[1]
                size = int(match[2]) * int(match[3]) if match[2] else 120 * 90
        if key not in result or size > result[key][0]:
            result[key] = (size, value)
    return [item[1] for item in result.values()][:30]


def element_photo(element, source):
    """Choose a valid lazy image or the largest srcset candidate, regardless of order."""
    candidates = []
    for entry in (element.get('data-srcset') or element.get('srcset') or '').split(','):
        parts = entry.strip().split()
        if not parts:
            continue
        value = valid_photo(parts[0], source)
        if not value:
            continue
        try:
            size = float(parts[1].rstrip('wx')) if len(parts) > 1 else 0
        except ValueError:
            size = 0
        candidates.append((size, value))
    if candidates:
        return max(candidates, key=lambda entry: entry[0])[1]
    return valid_photo(element.get('data-src'), source) or valid_photo(element.get('src'), source)


def item_photos(item, source):
    images = item.get('images') or item.get('photos') or []
    result = []
    for item in images if isinstance(images, list) else []:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get('url') or item.get('contentUrl') or item.get('original')
            if not isinstance(value, str):
                sized = []
                for size, url in item.items():
                    try:
                        width, height = map(int, size.split('x'))
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(url, str):
                        sized.append((width * height, url))
                value = max(sized)[1] if sized else ''
        else:
            continue
        if valid_photo(value, source):
            result.append(valid_photo(value, source))
    return unique_photos(result, source)


def gallery_link(value, source):
    return valid_photo(value, source) if re.search(r'\.(?:jpe?g|png|webp)(?:[?#]|$)|/i(?:mage)?[/?]|/get-(?:autoru(?:-[\w-]+)?|images)/', value or '', re.I) else ''


def page_photos(soup, source, primary=''):
    found = []
    selector = {'avito': '[data-marker="item-view/main-gallery"]',
        'drom': '[data-ftid*="bull-gallery"], [data-ftid*="bull-image"], [data-ftid*="bull-page_bull-gallery"]',
        'autoru': '.CardImageGallery, [class*="CardGallery"], [class*="ImageGallery"], [class*="PhotoGallery"]'}[source]
    galleries = soup.select(selector)
    gallery_ids = {id(gallery) for gallery in galleries}
    for gallery in galleries:
        if any(id(parent) in gallery_ids for parent in gallery.parents):
            continue  # The parent gallery already includes these elements.
        for element in ([gallery] if gallery.name in ('a', 'img') else []) + gallery.select('a[href], img'):
            value = gallery_link(element.get('href'), source) if element.name == 'a' else element_photo(element, source)
            parent_link = element.find_parent('a', href=True) if element.name == 'img' else None
            if parent_link and gallery_link(parent_link.get('href'), source):
                continue
            if value:
                found.append(value)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(html.unescape(script.get_text()))
        except (ValueError, TypeError):
            continue
        for item in payload if isinstance(payload, list) else [payload]:
            if not isinstance(item, dict) or item.get('@type') not in ('Product', 'Car', 'Vehicle'):
                continue
            values = item.get('image', [])
            for value in values if isinstance(values, list) else [values]:
                if isinstance(value, dict):
                    value = value.get('contentUrl') or value.get('url', '')
                if valid_photo(value, source):
                    found.append(valid_photo(value, source))
    if source == 'avito':
        for script in soup.select('script[data-mfe-state]'):
            try:
                payload = json.loads(html.unescape(script.get_text()))
            except (ValueError, TypeError):
                continue
            data = payload.get('loaderData', {}).get('data', {}) if isinstance(payload, dict) else {}
            for item in (data, data.get('item', {}), data.get('itemInfo', {})) if isinstance(data, dict) else []:
                if isinstance(item, dict):
                    found.extend(item_photos(item, source))
    return unique_photos(found or [primary], source)
