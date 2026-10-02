"""Avito HTTP transport and structured catalogue reader.

Independent implementation, informed by the response fields documented in
Duff89/parser_avito (91a5399f79bf035d3eb919d66ada00545571c2f1).
No external cookie service, paid requests or proxy rotation are enabled.
"""
import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from core.network import proxy_setting


def avito_proxy():
    # Explicit empty AVITO_PROXY_URL means direct, even if other sources use a proxy.
    return proxy_setting('AVITO_PROXY_URL' if 'AVITO_PROXY_URL' in os.environ else 'SOURCES_PROXY_URL')


def http_enabled():
    backend = os.getenv('AVITO_TRANSPORT', 'http').strip().lower()
    if backend not in ('http', 'browser'):
        raise ValueError('AVITO_TRANSPORT must be http or browser')
    return backend == 'http'


class AvitoClient:
    """One persistent session per worker, one request per call; blocks propagate."""
    def __init__(self):
        from curl_cffi.requests import Session
        self.session = Session(impersonate='chrome', trust_env=False, proxy=avito_proxy())
        self.session.headers.update({'Accept-Language': 'ru-RU,ru;q=0.9',
                                     'Referer': 'https://www.avito.ru/'})
        self.page_source = ''
        self.last_request = 0.0
        self.cookie_stamp = None

    def load_cookies(self):
        configured = os.getenv('AVITO_COOKIES_FILE', '').strip()
        if not configured:
            return
        path = Path(configured)
        stamp = path.stat().st_mtime_ns
        if stamp == self.cookie_stamp:
            return
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(payload, dict) and 'cookies' in payload:
            payload = payload['cookies']
        cookies = []
        if isinstance(payload, dict):
            cookies = [(key, value, '.avito.ru', '/') for key, value in payload.items()
                       if isinstance(value, str)]
        elif isinstance(payload, list):
            for item in payload:
                domain = item.get('domain', '.avito.ru')
                if domain.lstrip('.') != 'avito.ru' and not domain.endswith('.avito.ru'):
                    continue
                if item.get('name') and isinstance(item.get('value'), str):
                    cookies.append((item['name'], item['value'], domain, item.get('path', '/')))
        if not cookies:
            raise ValueError('AVITO_COOKIES_FILE contains no Avito cookies')
        self.session.cookies.clear()
        for name, value, domain, path_value in cookies:
            self.session.cookies.set(name, value, domain=domain, path=path_value)
        self.cookie_stamp = stamp

    def get(self, url):
        from parsers.sources import SourceBlocked, check_block
        from bs4 import BeautifulSoup
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('avito.ru', 'www.avito.ru', 'm.avito.ru'):
            raise ValueError('Expected HTTPS Avito URL')
        self.load_cookies()
        delay = max(0, 5 - (time.monotonic() - self.last_request))
        if delay:
            time.sleep(delay)
        self.page_source = ''
        try:
            # Do not forward cookies to an unexpected redirect destination.
            response = self.session.get(url, timeout=30, allow_redirects=False)
        finally:
            self.last_request = time.monotonic()
        if response.status_code in (403, 429, 439):
            raise SourceBlocked(f'avito: HTTP {response.status_code}; access restricted')
        if 300 <= response.status_code < 400:
            raise SourceBlocked('avito: redirected; browser access check required')
        response.raise_for_status()
        check_block(BeautifulSoup(response.text, 'html.parser'), 'avito')
        self.page_source = response.text

    def quit(self):
        self.session.close()


def catalog_items(payload):
    """Recognize catalogue envelopes only, never recommendation carousels."""
    if not isinstance(payload, dict):
        return None
    for key in ('catalog', 'result'):
        child = payload.get(key)
        if isinstance(child, dict):
            found = catalog_items(child)
            if found is not None:
                return found
    if isinstance(payload.get('items'), list):
        return payload['items']
    loader = payload.get('loaderData')
    if isinstance(loader, dict):
        return catalog_items(loader.get('data'))
    return None


def listing_image(item):
    gallery = item.get('gallery') or {}
    if isinstance(gallery, dict) and gallery.get('imageLargeUrl'):
        return gallery['imageLargeUrl']
    images = item.get('images') or []
    if images and isinstance(images[0], dict):
        choices = []
        for size, value in images[0].items():
            if not isinstance(value, str):
                continue
            dimensions = re.fullmatch(r'(\d+)x(\d+)', size)
            choices.append((int(dimensions[1])*int(dimensions[2]) if dimensions else 0, value))
        if choices:
            return max(choices)[1]
    return ''


def normalize_item(item):
    from parsers.sources import number
    if not isinstance(item, dict):
        return None
    identifier = item.get('id')
    name, path = item.get('title'), item.get('urlPath')
    if not str(identifier or '').isdigit() or not isinstance(name, str) or not isinstance(path, str):
        return None
    link = urljoin('https://www.avito.ru', path).split('?')[0]
    if urlparse(link).hostname not in ('www.avito.ru', 'avito.ru') or '/avtomobili/' not in urlparse(link).path:
        return None
    price = item.get('priceDetailed') or {}
    price = number(price.get('value')) if isinstance(price, dict) else None
    if not price:
        return None
    stem = re.split(r',\s*(?:19|20)\d{2}', name)[0]
    brand, _, model = stem.partition(' ')
    # Multi-word makes must not shift a word into the model.
    for make in ('Land Rover', 'Great Wall', 'Alfa Romeo', 'Aston Martin', 'Rolls Royce'):
        if stem.lower().startswith(make.lower() + ' '):
            brand, model = make, stem[len(make)+1:]
            break
    location = item.get('location') or {}
    address = item.get('addressDetailed') or {}
    city = location.get('name', '') if isinstance(location, dict) else ''
    if not city and isinstance(address, dict):
        city = address.get('locationName', '')
    year = re.search(r'\b((?:19|20)\d{2})\b', name)
    mileage = re.search(r'([\d\s\u00a0]+)\s*км\b', name)
    return {'platform': 'avito', 'external_id': str(identifier), 'link': link,
            'name': name, 'brand': brand, 'model': model, 'price': price,
            'year': year[1] if year else '', 'city': city,
            'image': urljoin('https://www.avito.ru', listing_image(item)) if listing_image(item) else '',
            'mileage': number(mileage[1]) if mileage else None,
            'generation': '', 'generation_codes': [], 'ad_type': 'unknown'}


def structured_listings(soup):
    for script in soup.select('script[data-mfe-state], script[type="application/json"]'):
        try:
            payload = json.loads(html.unescape(script.get_text()))
        except (ValueError, TypeError):
            continue
        items = catalog_items(payload)
        if items is None:
            continue
        ads = {}
        for item in items:
            ad = normalize_item(item)
            if ad:
                ads[ad['external_id']] = ad
        if ads:
            return list(ads.values())
        # Fall back to DOM for unrecognized nonempty data, not a fake empty result.
        if not items:
            return []
    return None
