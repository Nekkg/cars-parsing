from copy import deepcopy
from ads.filter_values import bounds
from ads.catalog_search import normalize

CITIES = dict(zip(
    ('москва','санктпетербург','новосибирск','екатеринбург','казань','нижнийновгород','челябинск','самара','омск','ростовнадону','уфа','красноярск','воронеж','пермь','волгоград','краснодар','саратов','тюмень','тольятти','ижевск','барнаул','ульяновск','иркутск','хабаровск','ярославль','владивосток','махачкала','томск','оренбург','кемерово','новокузнецк','рязань','астрахань','пенза','липецк','киров'),
    ('moskva','sankt-peterburg','novosibirsk','ekaterinburg','kazan','nizhniy-novgorod','chelyabinsk','samara','omsk','rostov-na-donu','ufa','krasnoyarsk','voronezh','perm','volgograd','krasnodar','saratov','tyumen','tolyatti','izhevsk','barnaul','ulyanovsk','irkutsk','khabarovsk','yaroslavl','vladivostok','makhachkala','tomsk','orenburg','kemerovo','novokuznetsk','ryazan','astrakhan','penza','lipetsk','kirov')))


def city_slug(filters, source):
    cities = filters.get('city') or []
    slug = CITIES.get(normalize(cities[0])) if len(cities) == 1 else None
    if source == 'avito' and slug:
        return slug.replace('-', '_').replace('khabarovsk','habarovsk')
    if source == 'drom':
        return {'moskva':'moscow','sankt-peterburg':'spb','nizhniy-novgorod':'nizhniy-novgorod'}.get(slug,slug)
    return {'moskva':'moskva','sankt-peterburg':'sankt-peterburg'}.get(slug,slug)


def variants(filters):
    cities = filters.get('city') or [None]
    makes = [None] if filters.get('vehicle',{}).get('make') else (filters.get('brand') or [None])
    output=[]
    for city in cities:
        for make in makes:
            data=deepcopy(filters)
            data['city']=[city] if city and city != 'unknown' else []
            if make and make != 'unknown':
                from ads.models import CatalogNode
                from ads.catalog_search import ranked_rows
                found=ranked_rows(list(CatalogNode.objects.filter(kind='make')),make)
                if found:
                    data['vehicle']={'make':{'name':found[0].name,'slug':found[0].slug}}
            output.append(data)
    return output


# Numeric-range envelope captured from Avito's current public UI (October 2026).
# Attribute 164669 is the production year; no make/model enum is embedded here.
_AVITO_YEAR_HEADER = bytes.fromhex('01 28 02 02 01 02 01 45 fa 8c 14')


def avito_year_filter(lo, hi, *, facets=None):
    if lo is None and hi is None:
        return None
    if any(value is not None and (type(value) is not int or not 0 <= value <= 9999)
           for value in (lo, hi)) or lo is not None and hi is not None and lo > hi:
        raise ValueError('Некорректный диапазон годов выпуска.')
    import base64
    import json
    values = {key: value for key, value in (('from', lo), ('to', hi)) if value is not None}
    payload = json.dumps(values, separators=(',', ':')).encode('ascii')
    # Four-digit years keep the compact payload length below the varint boundary.
    numeric = _AVITO_YEAR_HEADER[6:] + bytes((len(payload),)) + payload
    if facets is None:
        encoded = _AVITO_YEAR_HEADER[:6] + numeric
    else:
        try:
            enums = base64.b64decode(facets + '=' * (-len(facets) % 4), altchars=b'-_', validate=True)
        except (ValueError, TypeError):
            raise RuntimeError('Unrecognized Avito vehicle filter') from None
        # Exact public UI envelope: one enum section (make, optionally model).
        # Setting the numeric-section flag retains both vehicle facets and years.
        if not (8 <= len(enums) <= 64 and enums[:6] == bytes.fromhex('01 28 01 02 02 02')
                and enums[6] in (1, 2) and enums[7] == 0x44):
            raise RuntimeError('Unrecognized Avito vehicle filter')
        encoded = enums[:4] + b'\x01' + enums[5:] + numeric
    return base64.urlsafe_b64encode(encoded).decode('ascii').rstrip('=')


def avito_redirect_url(requested, target):
    """Keep range/page constraints when Avito canonicalizes a text vehicle query."""
    import base64
    import json
    import re
    from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode
    original = dict(parse_qsl(urlsplit(requested).query))
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query))
    # Generated tracking context describes the pre-canonical query. It is not a
    # search constraint and can be rejected when combined with the corrected f.
    query.pop('context', None)
    for key in ('s', 'p', 'pmin', 'pmax', 'localPriority', 'radius', 'searchRadius'):
        if key in original:
            query[key] = original[key]
    token = original.get('f')
    if token:
        try:
            raw = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        except ValueError:
            raise RuntimeError('Unrecognized Avito year filter') from None
        if raw.startswith(_AVITO_YEAR_HEADER):
            size = raw[len(_AVITO_YEAR_HEADER)]
            payload = raw[len(_AVITO_YEAR_HEADER)+1:]
            if len(payload) != size:
                raise RuntimeError('Unrecognized Avito year filter')
            values = json.loads(payload)
            facet = re.search(r'-(ASg[A-Za-z0-9_-]+)$', parts.path.rsplit('/', 1)[-1])
            if facet:
                query['f'] = avito_year_filter(values.get('from'), values.get('to'), facets=facet[1])
            elif parts.path.rstrip('/').endswith('/avtomobili'):
                query['f'] = token
            else:
                raise RuntimeError('Avito redirect could not preserve year filter')
        else:
            query['f'] = token
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def numeric_params(filters, source):
    from ads.vehicle_selection import effective_year_bounds
    year_lo, year_hi = effective_year_bounds(filters)
    if year_lo is not None and year_hi is not None and year_lo > year_hi:
        raise ValueError('Выбранное поколение не пересекается с указанными годами.')
    params = {}
    mapping = {'drom': {'price': ('minprice', 'maxprice'), 'year': ('minyear', 'maxyear')},
               'autoru': {'price': ('price_from', 'price_to'), 'year': ('year_from', 'year_to')},
               'avito': {'price': ('pmin', 'pmax')}}[source]
    for field, names in mapping.items():
        lo, hi = (year_lo, year_hi) if field == 'year' else bounds(filters.get(field))
        for name, value in zip(names, (lo, hi)):
            if value is not None:
                params[name] = value
    if source == 'avito':
        token = avito_year_filter(year_lo, year_hi)
        if token:
            params['f'] = token
    return params


def hydrate_generation_years(filters):
    """Old tracked snapshots may retain only catalogue keys; add public year metadata."""
    from ads.vehicle_selection import selected_generations
    keys=filters.get('generation_keys')
    model=filters.get('vehicle',{}).get('model',{})
    if selected_generations(filters) or filters.get('generation_years') or not keys or not model.get('key'):return filters
    from ads.models import CatalogNode
    nodes=CatalogNode.objects.filter(kind='generation',parent__key=model['key'],key__in=keys)
    generations=[{'name':node.name,'slug':node.slug,'key':node.key,**node.data} for node in nodes]
    return {**filters,'generation_years':generations}
