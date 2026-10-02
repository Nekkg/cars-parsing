"""Listing and detail extraction. Source blocks are explicit errors, never empty results."""
import re
from urllib.parse import urlparse, urljoin, urlencode
from bs4 import BeautifulSoup
from .query_filters import city_slug, numeric_params


class SourceBlocked(RuntimeError):
    pass


def number(value):
    match = re.search(r'\d[\d\s\u00a0]*', str(value or ''))
    digits = re.sub(r'\D', '', match[0]) if match else ''
    return int(digits) if digits else None


def text(node, selectors):
    found = node.select_one(selectors)
    return found.get_text(' ', strip=True) if found else ''


def source_url(source, filters, page=1):
    vehicle = filters.get('vehicle', {})
    make, model = vehicle.get('make', {}), vehicle.get('model', {})
    city = city_slug(filters,source)
    params = numeric_params(filters,source)
    if source == 'avito':
        query = ' '.join((make.get('name',''), filters.get('variant') or model.get('name',''))).strip()
        return f'https://www.avito.ru/{city or "all"}/avtomobili?' + urlencode({'s':104, 'p':page, 'q':query, **params})
    if source == 'drom':
        path = '/'.join(n['slug'] for n in (make, model) if n.get('slug')) or 'all'
        host = f'{city}.drom.ru' if city else 'auto.drom.ru'
        return f'https://{host}/{path}/' + (f'page{page}/' if page > 1 else '') + '?' + urlencode({'unsold':1, **params})
    aliases = {'mercedes-benz':'mercedes', 'lada':'vaz', 'land_rover':'land_rover'}
    make_slug = aliases.get(make.get('slug'), make.get('slug', ''))
    model_slug = model.get('slug', '').replace('-', '_')
    if make_slug == 'bmw':
        model_slug = re.sub(r'^(\d)_series$',r'\1er',model_slug)
    if make_slug == 'mercedes':
        model_slug = model_slug.replace('_class','_klasse')
    path = '/'.join(p for p in (make_slug, model_slug) if p)
    if path:
        return f'https://auto.ru/{city or "rossiya"}/cars/{path}/all/?' + urlencode({'sort':'cr_date-desc','page':page, **params})
    return f'https://auto.ru/{city or "rossiya"}/cars/all/?' + urlencode({'sort':'cr_date-desc','page':page, **params})


def check_block(soup, source):
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    beginning = soup.get_text(' ', strip=True)[:1800].lower()
    if any(word in beginning for word in ('доступ ограничен', 'подтвердите, что вы не робот', 'подтвердите, что вы человек', 'пройдите проверку', 'smartcaptcha')):
        raise SourceBlocked(f'{source}: ограничение доступа/капча. Откройте браузер проверки на http://localhost:7900/ (пароль secret).')
    if 'captcha' in title.lower():
        raise SourceBlocked(f'{source}: капча, требуется ручная проверка браузера.')


def extract_listings(html, source):
    soup = BeautifulSoup(html, 'html.parser')
    check_block(soup, source)
    if source == 'avito':
        from .avito_client import structured_listings
        structured = structured_listings(soup)
        if structured is not None:
            return structured
    selectors = {'drom':'[data-ftid="bulls-list_bull"]', 'avito':'[data-marker="item"]',
                 'autoru':'[data-seo="listing-item"], .ListingItem'}
    cards = soup.select(selectors[source])
    ads = []
    for card in cards:
        link_node = card.select_one({'drom':'a[data-ftid="bull_title"]',
            'avito':'a[data-marker="item-title"]', 'autoru':'a.ListingItemTitle__link, a[href*="/sale/"], a[href*="/group/"]'}[source])
        if not link_node:
            continue
        link = urljoin({'drom':'https://auto.drom.ru','avito':'https://www.avito.ru','autoru':'https://auto.ru'}[source],link_node.get('href','')).split('?')[0]
        match = re.search(r'(\d{6,})(?:-[\w-]+)?(?:\.html)?/?$', link)
        if not match:
            continue
        title = link_node.get_text(' ',strip=True)
        ad = {'platform':source, 'link':link, 'external_id':match[1], 'name':title,
              'generation':'', 'generation_codes':[], 'ad_type':'unknown'}
        year = re.search(r'\b((?:19|20)\d{2})\b', title)
        ad['year'] = year[1] if year else text(card,'.ListingItem__year')
        ad['price'] = number(text(card, '[data-ftid="bull_price"], [data-marker="item-price"], .ListingItemPrice__content, [class*="ListingItemUniversalPrice__title"]'))
        image = card.select_one('img[src]')
        ad['image'] = image.get('src','') if image else ''
        if image and image.get('srcset'):
            ad['image'] = image['srcset'].split(',')[-1].strip().split(' ')[0]
        if ad['image'].startswith('//'): ad['image'] = 'https:' + ad['image']
        ad['city'] = text(card, '[data-ftid="bull_location"], [data-marker="item-address"], [data-marker="item-location"], [itemprop="addressLocality"], [class*="geo-root"], .MetroListPlace__regionName, .ListingItem__place')
        if source == 'drom':
            parts = urlparse(link).path.strip('/').split('/')
            ad['brand'], ad['model'] = parts[-3:-1]
        elif source == 'autoru':
            parts = urlparse(link).path.strip('/').split('/')
            ad['brand'], ad['model'] = parts[-3:-1]
            if ad['brand'] == 'bmw': ad['model'] = re.sub(r'^(\d)er$',r'\1-series',ad['model'])
            if ad['brand'] == 'mercedes':
                ad['brand'] = 'mercedes-benz'
                ad['model'] = ad['model'].replace('_klasse','-class')
            generation = re.search(r'\b[IVX]+\s*\([^)]*\)(?:\s*Рестайлинг)?',title,re.I)
            if generation:
                ad['generation'] = generation[0]
                codes = re.search(r'\(([^)]*)\)',generation[0])
                ad['generation_codes'] = [code.strip() for code in re.split(r'[,/]',codes[1]) if code.strip()]
        else:
            stem = re.split(r',\s*(?:19|20)\d{2}',title)[0]
            parts = stem.split(' ',1)
            ad['brand'], ad['model'] = (parts+[''])[:2]
        mileage = re.search(r'([\d\s\u00a0]+)\s*км\b',card.get_text(' ',strip=True))
        ad['mileage'] = number(mileage[1]) if mileage else None
        ads.append(ad)
    if not cards:
        if source == 'autoru' and soup.select_one('.ListingEmptyOffers'):
            return []  # End of listing; the carousel contains recommendations, not matches.
        body = soup.get_text(' ',strip=True).lower()
        if not any(s in body for s in ('объявления не найдены', 'ничего не найдено', 'нет объявлений', 'не нашли объявления')):
            raise RuntimeError(f'{source}: карточки не найдены; разметка изменилась или страница недоступна.')
    elif not ads:
        raise RuntimeError(f'{source}: карточки найдены, но ссылки не распознаны.')
    elif not any(ad.get('price') for ad in ads):
        raise RuntimeError(f'{source}: карточки найдены, но цены не распознаны; требуется обновить разбор страницы.')
    return ads


def enrich(ad, html, generations=()):
    soup = BeautifulSoup(html, 'html.parser')
    check_block(soup, ad['platform'])
    if ad['platform'] == 'avito':
        ad['city'] = text(soup,'[itemprop="addressLocality"], [data-marker="item-view/item-address"], [data-marker="item-view/location"], [data-marker="item-address"]') or ad.get('city','')
    specs = {}
    for row in soup.select('tr[data-ftid^="specification"], li[class*="CardInfoRow"], li[class*="CardInfoGroupedRow"], [data-marker="item-view/item-params"] li'):
        cells = row.select('th, td') or row.select('div[class*="cellTitle"], div[class*="cellValue"]')
        if len(cells) < 2:
            cells = row.find_all('div', recursive=False)
        if len(cells) >= 2:
            specs[cells[0].get_text(' ',strip=True).rstrip(':')] = cells[1].get_text(' ',strip=True)
        else:
            value = row.get_text(' ',strip=True).split(':',1)
            if len(value)==2:
                specs[value[0].strip()] = value[1].strip()
    mappings = {'engine':['Двигатель','Тип двигателя'], 'mileage':['Пробег','Пробег, км'],
        'gearbox':['Коробка передач'], 'drive':['Привод'], 'color':['Цвет'], 'body_type':['Кузов','Тип кузова'],
        'steering':['Руль'], 'owners':['Владельцы','Владельцев по ПТС'], 'year':['Год выпуска'],
        'brand':['Марка'], 'model':['Модель'], 'generation':['Поколение'], 'condition':['Состояние']}
    for field, labels in mappings.items():
        for label in labels:
            if specs.get(label):
                ad[field] = number(specs[label]) if field in ('mileage','owners') else specs[label]
                break
    if ad['platform'] == 'autoru':
        ad['name'] = text(soup,'h1.CardHead__title, h1') or ad['name']
        ad['price'] = number(text(soup,'.OfferPriceCaption__price')) or ad['price']
        ad['city'] = text(soup,'.MetroListPlace__regionName') or ad['city']
    if not ad.get('year'):
        year = re.search(r'\b((?:19|20)\d{2})\b',ad['name'])
        ad['year'] = year[1] if year else None
    generation_link = soup.select_one('[data-ftid="specification-generation"] a')
    # Exact catalogue links take priority over a body code shared by a facelift.
    generations = sorted(generations, key=lambda gen: not (generation_link and generation_link.get('href') in gen.data.get('generation_urls', [])))
    for gen in generations:
        known_url = generation_link and generation_link.get('href') in gen.data.get('generation_urls', [])
        # Only search the actual generation attribute, never advertisements or spare-parts banners.
        code_match = any(re.search(r'(?<!\w)'+re.escape(code)+r'(?!\w)',ad.get('generation',''),re.I)
                         for code in gen.data.get('codes', []))
        if known_url or code_match:
            ad['generation_codes'] = gen.data.get('codes', [])
            if known_url:
                ad['generation'] = gen.name
            break
    ad['gearbox'] = 'автомат' if ad.get('gearbox','').lower() == 'акпп' else ad.get('gearbox','')
    return ad
