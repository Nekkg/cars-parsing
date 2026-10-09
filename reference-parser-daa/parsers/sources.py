"""Listing and detail extraction. Source blocks are explicit errors, never empty results."""
import re
from urllib.parse import urlparse, urljoin, urlencode
from bs4 import BeautifulSoup
from .query_filters import city_slug, numeric_params


class SourceBlocked(RuntimeError):
    pass


class SourceTemporaryError(ConnectionError):
    """A bounded retry can recover this failure without treating it as an IP block."""


class SourceUnavailable(SourceTemporaryError):
    """Controlled transient source response; never interpreted as an IP block."""
    def __init__(self, source, stage, status):
        self.status = status
        super().__init__(f'{source}: {stage} endpoint HTTP {status}')


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
        model_name = filters.get('variant') or model.get('name', '')
        if not filters.get('variant'):
            # These badges use a shorter official spelling on the marketplace.
            if make.get('slug') == 'mazda':
                model_name = re.sub(r'^Mazda(?=\d)', '', model_name, flags=re.I)
            if make.get('slug') == 'changan' and model.get('slug','').startswith('qiyuan_'):
                from .source_names import MODEL_ALIASES
                names = MODEL_ALIASES.get('changan/'+model.get('slug',''), ())
                if names: model_name = names[0]
        if make.get('slug') == 'lexus' and not filters.get('variant'):
            from .model_names import lexus_badge
            badge = lexus_badge(model.get('slug', ''))
            if badge: model_name = badge[0].upper()
        if make.get('slug') == 'infiniti' and not filters.get('variant'):
            from .model_names import infiniti_badge
            badge=infiniti_badge(model.get('slug',''))
            if badge:model_name=badge[0].upper()
        query = ' '.join((make.get('name',''), model_name)).strip()
        radius = filters.get('radius_km')
        if radius and city:
            if type(radius) is not int or not 1 <= radius <= 1000:
                raise ValueError('Некорректный радиус поиска.')
            params.update(localPriority=1, radius=radius, searchRadius=radius)
        return f'https://www.avito.ru/{city or "all"}/avtomobili?' + urlencode({'s':104, 'p':page, 'q':query, **params})
    if source == 'drom':
        path = '/'.join(n['slug'] for n in (make, model) if n.get('slug')) or 'all'
        host = f'{city}.drom.ru' if city else 'auto.drom.ru'
        return f'https://{host}/{path}/' + (f'page{page}/' if page > 1 else '') + '?' + urlencode({'unsold':1, **params})
    from .source_names import binding, source_model_slug
    route = binding('autoru',make.get('key') or make.get('slug',''),model.get('key') or (make.get('slug','')+'/'+model['slug'] if model.get('slug') else None))
    aliases = {'mercedes-benz':'mercedes', 'lada':'vaz', 'land_rover':'land_rover', 'cheryexeed':'exeed', 'solaris-agr':'solaris', 'volga-mark':'volga'}
    make_slug = aliases.get(make.get('slug'), make.get('slug', ''))
    model_slug = source_model_slug(make.get('slug',''),model.get('slug',''))
    if make_slug == 'lexus':
        from .model_names import lexus_badge
        badge = lexus_badge(model_slug)
        if badge: model_slug = badge[0]
    if make_slug == 'bmw':
        model_slug = re.sub(r'^(\d)_series$',r'\1er',model_slug)
    if make_slug == 'mercedes':
        model_slug = model_slug.replace('_class','_klasse')
    if route:
        make_slug,model_slug=route['make_slug'],route.get('model_slug','')
    path = '/'.join(p for p in (make_slug, model_slug) if p)
    if path:
        return f'https://auto.ru/{city or "rossiya"}/cars/{path}/all/?' + urlencode({'sort':'cr_date-desc','page':page, **params})
    return f'https://auto.ru/{city or "rossiya"}/cars/all/?' + urlencode({'sort':'cr_date-desc','page':page, **params})


def check_block(soup, source):
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    beginning = soup.get_text(' ', strip=True)[:1800].lower()
    if any(word in beginning for word in ('доступ ограничен', 'подтвердите, что вы не робот', 'подтвердите, что вы человек', 'пройдите проверку', 'smartcaptcha')):
        raise SourceBlocked(f'{source}: access restricted')
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
        if source == 'autoru':
            from .photos import element_photo
            candidates = card.select('.ListingItemThumb img, [class*="Gallery"] img') or card.select('img')
            ad['image'] = next((photo for image in candidates if (photo := element_photo(image, source))), '')
        if source == 'avito':
            # SVG/loading placeholders and seller avatars are not car photographs.
            ad['image'] = ''
            for candidate in card.select('img[src]'):
                candidate_url = candidate.get('src', '')
                host = urlparse(candidate_url).hostname or ''
                if urlparse(candidate_url).scheme == 'https' and host.endswith('.img.avito.st'):
                    ad['image'] = candidate_url
                    break
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
            from .generations import title_generation
            ad['generation'] = title_generation(title)
            codes = re.search(r'\(([^)]*)\)', ad['generation'])
            if codes and not re.search(r'(?:19|20)\d{2}', codes[1]):
                ad['generation_codes'] = [code.strip() for code in re.split(r'[,/]', codes[1]) if code.strip()]
        else:
            from .avito_client import vehicle_name
            ad['brand'], ad['model'] = vehicle_name(title)
            from .avito_client import listing_characteristics
            ad.update(listing_characteristics(title, card))
        mileage = re.search(r'([\d\s\u00a0]+)\s*км\b',card.get_text(' ',strip=True))
        ad['mileage'] = number(mileage[1]) if mileage else None
        from .date_values import source_datetime
        stamp = card.select_one('time[datetime], [itemprop=datePosted][content]')
        published = source_datetime(stamp.get('datetime') or stamp.get('content')) if stamp else None
        if published:
            ad['published_at'] = published.isoformat()
        ads.append(ad)
    if not cards:
        if source == 'autoru':
            empty=any('нет предложений' in header.get_text(' ',strip=True).lower()
                for header in soup.select('.ListingFilterPanel, .ListingHead, [class*=ListingCarsFiltersDumb-]'))
            if soup.select_one('.ListingEmptyOffers') or empty:
                return []  # Recommendations/dealer carousels are not matching offers.
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
    if ad['platform'] == 'avito' and not ad.get('image'):
        gallery = soup.select_one('[data-marker="item-view/main-gallery"]')
        if gallery:
            for image in gallery.select('img[src]'):
                image_url = image.get('src', '')
                parsed_image = urlparse(image_url)
                if parsed_image.scheme == 'https' and (parsed_image.hostname or '').endswith('.img.avito.st'):
                    ad['image'] = image_url
                    break
    from .photos import page_photos
    ad['images'] = page_photos(soup,ad['platform'],ad.get('image','')) or ad.get('images',[])
    if ad['images']: ad['image']=ad['images'][0]
    specs = {}
    for row in soup.select('tr[data-ftid^="specification"], li[class*="CardInfoRow"], li[class*="CardInfoGroupedRow"], li[class*="CardInfoSummarySimpleRow-"], li[class*="CardInfoSummaryComplexRow-"], [data-marker="item-view/item-params"] li'):
        cells = row.select('th, td') or row.select('div[class*="cellTitle"], div[class*="cellValue"]')
        if len(cells) < 2:
            cells = row.find_all('div', recursive=False)
        if len(cells) >= 2:
            specs[cells[0].get_text(' ',strip=True).rstrip(':')] = cells[1].get_text(' ',strip=True)
        else:
            value = row.get_text(' ',strip=True).split(':',1)
            if len(value)==2:
                specs[value[0].strip()] = value[1].strip()
    ad['source_modification'] = specs.get('Модификация', '')[:300]
    mappings = {'engine':['Двигатель','Тип двигателя'], 'mileage':['Пробег','Пробег, км'],
        'gearbox':['Коробка передач','Коробка'], 'drive':['Привод'], 'color':['Цвет'], 'body_type':['Кузов','Тип кузова'],
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
    from .generations import normalize_generation
    normalize_generation(ad, generations, generation_link.get('href') if generation_link else None)
    ad['gearbox'] = 'автомат' if (ad.get('gearbox') or '').lower() == 'акпп' else ad.get('gearbox','')
    return ad
