"""Avito HTTP transport and structured catalogue reader.

Independent implementation, informed by the response fields documented in
Duff89/parser_avito (91a5399f79bf035d3eb919d66ada00545571c2f1).
No external cookie service, paid requests or proxy rotation are enabled.
"""
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from core.network import proxy_setting

logger = logging.getLogger(__name__)


def avito_proxy():
    # Explicit empty AVITO_PROXY_URL means direct, even if other sources use a proxy.
    return proxy_setting('AVITO_PROXY_URL' if 'AVITO_PROXY_URL' in os.environ else 'SOURCES_PROXY_URL')


def http_enabled():
    backend = os.getenv('AVITO_TRANSPORT', 'http').strip().lower()
    if backend not in ('http', 'browser'):
        raise ValueError('AVITO_TRANSPORT must be http or browser')
    return backend == 'http'


class AvitoClient:
    """One persistent HTTP session per worker, with bounded local PoW recovery."""
    def __init__(self):
        from curl_cffi.requests import Session
        profile = os.getenv('AVITO_HTTP_PROFILE', 'firefox147').strip()
        from curl_cffi.requests.impersonate import BrowserType
        allowed = {item.value for item in BrowserType} | {'chrome', 'firefox', 'safari', 'edge'}
        if profile not in allowed:
            raise ValueError('Unsupported AVITO_HTTP_PROFILE')
        route = avito_proxy()
        self.session = Session(impersonate=profile, trust_env=False, proxy=route)
        from parsers.avito_session import AvitoCookieStore
        self.cookie_store = AvitoCookieStore(profile, route)
        restored = self.cookie_store.load(self.session.cookies)
        logger.info('Avito HTTP session: route=%s restored_cookies=%s',
                    'proxy' if route else 'direct', restored)
        self.session.headers.update({'Accept-Language': 'ru-RU,ru;q=0.9',
                                     'Referer': 'https://www.avito.ru/'})
        self.page_source = ''
        self.last_request = 0.0
        self.last_page_request = 0.0
        self.cookie_stamp = None
        self.catalogue_routes = {}

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

    @staticmethod
    def validate_url(url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('avito.ru', 'www.avito.ru', 'm.avito.ru') or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError('Expected HTTPS Avito URL')

    def request(self, method, url, *, deadline, _challenge=False, **kwargs):
        from parsers.sources import SourceBlocked, SourceTemporaryError
        if _challenge:
            from parsers.avito_captcha import validate_captcha_url
            validate_captcha_url(url)
            if method != 'get':
                raise ValueError('Only GET captcha resources are allowed')
        else:
            self.validate_url(url)
        verification = _challenge or urlparse(url).path in (
            '/web/3/firewallPow/get', '/web/3/firewallPow/verify',
            '/web/5/firewallCaptcha/get', '/web/3/firewallCaptcha/verify')
        # Limit page/card traffic, not every resource of a single verification.
        previous = self.last_request if verification else getattr(self, 'last_page_request', self.last_request)
        interval = 0.25 if verification else 5
        delay = max(0, interval - (time.monotonic() - previous))
        started = time.monotonic()
        stage = 'verification' if verification else 'page'
        remaining = deadline - time.monotonic() - delay
        if remaining <= 0:
            raise SourceTemporaryError('avito: request time budget exceeded')
        if delay:
            time.sleep(delay)
        from curl_cffi.requests.exceptions import ConnectionError, IncompleteRead, Timeout
        try:
            # Explicit same session, route, cookies and no automatic redirects.
            if method != 'get':
                try:
                    return getattr(self.session, method)(url, timeout=min(20, remaining),
                                                         allow_redirects=False, **kwargs)
                except (ConnectionError, IncompleteRead, Timeout) as error:
                    # Verification POSTs are not replayed blindly.
                    raise SourceTemporaryError(f'avito: verification transport failed ({type(error).__name__})') from None
            for attempt in range(2):
                response = None
                try:
                    response = self.session.get(url, timeout=min(20, remaining),
                                                allow_redirects=False, stream=True, **kwargs)
                    chunks, size = [], 0
                    try:
                        for chunk in response.iter_content():
                            size += len(chunk)
                            if size > 10_000_000:
                                raise SourceBlocked('avito: response size limit exceeded')
                            chunks.append(chunk)
                    except (ConnectionError, IncompleteRead, Timeout):
                        content = b''.join(chunks)
                        # Curl may signal a trailer error after the complete HTML.
                        # Accept only a complete document, never a truncated listing.
                        if response.status_code != 200 or not re.search(rb'</html>\s*$', content, re.I):
                            raise
                    response.content = b''.join(chunks)
                    return response
                except (ConnectionError, IncompleteRead, Timeout) as error:
                    if attempt:
                        raise SourceTemporaryError(f'avito: GET transport failed ({type(error).__name__})') from None
                    remaining = deadline - time.monotonic() - 5
                    if remaining <= 0:
                        raise SourceTemporaryError('avito: transport retry budget exceeded') from None
                    time.sleep(5)
                finally:
                    if response is not None:
                        response.close()
        finally:
            self.last_request = time.monotonic()
            if not verification:
                self.last_page_request = self.last_request
            logger.info('Avito HTTP: stage=%s method=%s elapsed=%.2fs',
                        stage, method, self.last_request - started)

    def recover_pow(self, response, url, *, deadline):
        from parsers.avito_pow import pow_challenge, result, solution
        from parsers.sources import SourceBlocked
        challenge = pow_challenge(response, self.session.cookies)
        if not challenge:
            return False
        headers = {'Accept': 'application/json', 'Origin': 'https://www.avito.ru',
                   'Referer': url, 'Sec-Fetch-Dest': 'empty',
                   'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin'}
        origin = 'https://www.avito.ru/web/3/firewallPow/'
        task = result(self.request('post', origin + 'get', deadline=deadline,
                                   headers=headers, json={'challenge': challenge}))
        work = solution(task.get('challenge_jwt'), seconds=deadline-time.monotonic())
        verified = result(self.request('post', origin + 'verify', deadline=deadline,
                                       headers=headers, json=work))
        if verified.get('verified') is not True:
            raise SourceBlocked('avito: PoW verification rejected')
        logger.info('Avito local PoW verification accepted')
        return True

    def get(self, url):
        from parsers.sources import SourceBlocked, SourceUnavailable, check_block
        from bs4 import BeautifulSoup
        self.validate_url(url)
        self.load_cookies()
        self.page_source = ''
        requested = url
        from urllib.parse import parse_qs, urlsplit, urlunsplit
        from parsers.query_filters import avito_redirect_url
        original = urlsplit(requested)
        query = parse_qs(original.query).get('q', [''])[0].strip()
        route_key = (original.netloc, original.path, query)
        routes = getattr(self, 'catalogue_routes', {})
        cached = routes.get(route_key) if query else None
        if cached and time.monotonic() - cached[0] < 3600:
            url = avito_redirect_url(requested, cached[1])
        deadline = time.monotonic() + 90
        visited, recoveries, captcha_recoveries = set(), 0, 0
        for _ in range(4):
            if url in visited:
                raise SourceBlocked('avito: redirect loop')
            visited.add(url)
            while True:
                response = self.request('get', url, deadline=deadline)
                logger.info('Avito page response: HTTP=%s', response.status_code)
                if response.status_code not in (403, 429, 439):
                    break
                if recoveries < 2 and self.recover_pow(response, url, deadline=deadline):
                    recoveries += 1
                    continue  # Retry the exact URL, including city, query and page.
                from parsers.avito_captcha import recover_captcha
                if captcha_recoveries < 2 and recover_captcha(self, response, url, deadline):
                    captcha_recoveries += 1
                    continue
                raise SourceBlocked(f'avito: HTTP {response.status_code}; access restricted after bounded recovery')
            target = response.headers.get('Location') if 300 <= response.status_code < 400 else None
            if 500 <= response.status_code <= 599:
                raise SourceUnavailable('avito', 'page', response.status_code)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            check_block(soup, 'avito')
            target = target or ssr_redirect(soup)
            if target:
                target = urljoin(url, target)
                try:
                    self.validate_url(target)
                except ValueError:
                    raise SourceBlocked('avito: unsafe redirect rejected') from None
                from parsers.query_filters import avito_redirect_url
                url = avito_redirect_url(requested, target)
                continue
            self.page_source = response.text
            resolved = urlsplit(url)
            if query and '/avtomobili/' in resolved.path and resolved.path != original.path:
                if len(routes) >= 128 and route_key not in routes:
                    routes.pop(next(iter(routes)))
                routes[route_key] = (time.monotonic(), urlunsplit((resolved.scheme, resolved.netloc, resolved.path, '', '')))
            store = getattr(self, 'cookie_store', None)
            if store is not None:
                store.save(self.session.cookies)
            return
        raise SourceBlocked('avito: too many redirects')

    def quit(self):
        self.session.close()


def ssr_redirect(soup):
    """Avito can encode a canonical search redirect in a successful HTML response."""
    for script in soup.select('script[data-mfe-state], script[type="application/json"]'):
        try:
            payload = json.loads(html.unescape(script.get_text()))
        except (ValueError, TypeError):
            continue
        loader = payload.get('loaderData', {}) if isinstance(payload, dict) else {}
        if not isinstance(loader, dict):
            continue
        target = loader.get('redirect')
        if isinstance(target, str) and target:
            return target
        data = loader.get('data', {})
        if isinstance(data, dict) and data.get('redirected') and isinstance(data.get('url'), str):
            return data['url']
    return None


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


def vehicle_name(title):
    stem = re.split(r',\s*(?:19|20)\d{2}', title)[0]
    brand, _, model = stem.partition(' ')
    # Avito includes the Latin badge in the make, not in the model name.
    lada = re.match(r'^(?:ВАЗ\s*\(\s*LADA\s*\)|LADA\s*\(\s*ВАЗ\s*\))\s+(.+)$', stem, re.I)
    if lada:
        brand, model = 'lada', lada[1]
    from .source_names import make_title_names
    from .generations import prefix
    defaults=('Land Rover','Great Wall','Alfa Romeo','Aston Martin','Rolls Royce','Lynk & Co','Li Auto',
        'Polar Stone','Rising Auto','DW Hower','TENET Plus','IM Motors','Renault Samsung')
    for make in (() if lada else sorted({*defaults,*make_title_names()},key=len,reverse=True)):
        found=prefix(stem,make)
        if found and stem[found.end():].strip():
            brand,model=stem[:found.end()].strip(),stem[found.end():].strip()
            break
    model = re.split(r'\s+\d+(?:\.\d+)?\s+(?:AT|MT|CVT|AMT|DCT)\b', model, flags=re.I)[0].strip()
    if brand.lower() == 'bmw':
        model = re.sub(r'^([1-8])\s+(?:\u0441\u0435\u0440\u0438\u044f|series)$', r'\1-series', model, flags=re.I)
    if brand.lower() == 'mercedes-benz':
        model = re.sub(r'\s+\u043a\u043b\u0430\u0441\u0441$', '-Class', model, flags=re.I)
    return brand, model


def listing_characteristics(title, card=None):
    """Only explicit listing characteristics, never generation guesses or seller prose."""
    values = {}
    gearbox = re.search(r'\b\d(?:[.,]\d{1,2})?\s+(AMT|CVT|DCT|AT|MT)\b', title, re.I)
    if gearbox:
        values['gearbox'] = {'AT':'автомат', 'MT':'механика', 'AMT':'робот',
                             'DCT':'робот', 'CVT':'вариатор'}[gearbox[1].upper()]
    if card is None:
        return values
    nodes = card.select('[data-marker="item-specific-params"], [class*="iva-item-autoParamsStep"]')
    for node in nodes:
        for part in re.split(r'[,;·\n]', node.get_text(' ', strip=True).lower()):
            part = part.strip()
            fuel = re.findall(r'\b(?:бензин|дизель|гибрид|газ|электро(?:мобиль)?)\b', part)
            if fuel:
                fuels = [*values.get('engine', '').split(', '), *fuel]
                values['engine'] = ', '.join(dict.fromkeys(value for value in fuels if value))
            drive = re.fullmatch(r'(передний|задний|полный)(?: привод)?', part)
            if drive:
                values['drive'] = drive[1]
            body = re.fullmatch(r'(седан|универсал|х[еэ]тчбек|кабриолет|внедорожник|минивэн|купе|пикап|микроавтобус|лифтбек|фургон)(?:\s+\d\s*дв\.?)?', part)
            if body:
                values['body_type'] = body[1].replace('хэтчбек', 'хетчбек')
    return values


def merge_listing_characteristics(ads, soup):
    for card in soup.select('[data-marker="item"]'):
        anchor = card.select_one('a[data-marker="item-title"]')
        if anchor is None:
            continue
        link = urljoin('https://www.avito.ru', anchor.get('href', '')).split('?')[0]
        identifier = re.search(r'_(\d{6,})/?$', link)
        ad = ads.get(identifier[1]) if identifier else None
        if ad and link == ad['link']:
            ad.update(listing_characteristics(ad['name'], card))


def normalize_item(item):
    from parsers.sources import number
    from parsers.photos import item_photos
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
    brand, model = vehicle_name(name)
    location = item.get('location') or {}
    address = item.get('addressDetailed') or {}
    city = location.get('name', '') if isinstance(location, dict) else ''
    if not city and isinstance(address, dict):
        city = address.get('locationName', '')
    year = re.search(r'\b((?:19|20)\d{2})\b', name)
    mileage = re.search(r'([\d\s\u00a0]+)\s*км\b', name)
    from .date_values import source_datetime
    published = source_datetime(item.get('sortTimeStamp'))
    ad = {'platform': 'avito', 'external_id': str(identifier), 'link': link,
            'published_at': published.isoformat() if published else None,
            'name': name, 'brand': brand, 'model': model, 'price': price,
            'year': year[1] if year else '', 'city': city,
            'image': urljoin('https://www.avito.ru', listing_image(item)) if listing_image(item) else '',
            'images': item_photos(item,'avito'),
            'mileage': number(mileage[1]) if mileage else None,
            'generation': '', 'generation_codes': [], 'ad_type': 'unknown'}
    ad.update(listing_characteristics(name))
    return ad


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
            merge_listing_characteristics(ads, soup)
            return list(ads.values())
        # Fall back to DOM for unrecognized nonempty data, not a fake empty result.
        if not items:
            return []
    return None
