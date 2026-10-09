"""Local city-centre radius matching; no runtime geocoding service."""
import csv
import math
import re
from functools import lru_cache
from pathlib import Path

ALIASES = {'мск':'Москва','питер':'Санкт-Петербург','спб':'Санкт-Петербург',
           'санкт петербург':'Санкт-Петербург','екб':'Екатеринбург','нн':'Нижний Новгород'}


def norm(value):
    return re.sub(r'[^\w]+','',str(value).casefold().replace('ё','е'))


@lru_cache(maxsize=1)
def cities():
    with (Path(__file__).parent/'data/russian_cities.csv').open(encoding='utf-8-sig',newline='') as stream:
        result = []
        for row in csv.DictReader(stream):
            name = row['city'] or row['settlement'] or row['region']
            try:
                lat, lon = float(row['geo_lat']),float(row['geo_lon'])
            except ValueError:
                continue
            result.append({'id':row['fias_id'],'name':name,'region':row['region'],
                           'latitude':lat,'longitude':lon})
        return tuple(result)


@lru_cache(maxsize=1)
def names():
    result = {}
    for city in cities():
        aliases = [city['name']]
        if city['name'].endswith('а'): aliases.append(city['name'][:-1]+'е')
        if city['name'].endswith('ь'): aliases.append(city['name'][:-1]+'и')
        for alias in aliases:
            result.setdefault(alias.casefold().replace('ё','е'),[]).append(city)
    for alias, target in ALIASES.items():
        result[alias] = [city for city in cities() if city['name']==target]
    return result


@lru_cache(maxsize=1)
def city_pattern():
    return re.compile(r'(?<!\w)('+ '|'.join(re.escape(name) for name in sorted(names(),key=len,reverse=True))+r')(?!\w)')


@lru_cache(maxsize=8192)
def find_city(text):
    text = text.casefold().replace('ё','е')
    match = city_pattern().search(text)
    if match:
        choices = names()[match[1]]
        regional = [city for city in choices if city['region'].casefold() in text]
        return regional or choices,match.span()
    return [],None


def extract_radius(text):
    """The same optional +km syntax in complete queries and the city filter."""
    matches = list(re.finditer(r'(?<!\d)\+\s*(\d{1,4})\s*(?:км)?\b', text))
    if len(matches) > 1:
        raise ValueError('Укажите один радиус, например Самара +200.')
    if not matches:
        return text, 0
    match = matches[0]
    radius = int(match[1])
    if not 1 <= radius <= 1000:
        raise ValueError('Радиус должен быть от 1 до 1000 км.')
    return text[:match.start()]+' '+text[match.end():], radius


def extract_location(text, radius=0):
    choices, span = find_city(text)
    remaining = text
    result = {'city': [], 'location': {}, 'radius_km': radius, 'city_choices': []}
    if choices:
        remaining = text[:span[0]]+' '+text[span[1]:]
        if len(choices) == 1:
            result.update(city=[choices[0]['name']], location=choices[0])
        else:
            result['city_choices'] = choices
        if find_city(remaining)[0]:
            raise ValueError('Для одного поиска укажите один город и, при необходимости, радиус.')
    elif radius:
        raise ValueError('Для радиуса укажите город: самара +200.')
    return result, remaining


def parse_city_input(text, default_city=None):
    text = text.strip().casefold().replace('ё', 'е')
    remaining, radius = extract_radius(text)
    if radius and not remaining.strip() and default_city:
        if isinstance(default_city, dict):
            return {'city': [default_city['name']], 'location': dict(default_city),
                    'radius_km': radius, 'city_choices': []}
        remaining = default_city
    result, remaining = extract_location(remaining, radius)
    choices = result['city_choices'] or ([result['location']] if result['location'] else [])
    if not choices:
        raise ValueError('Не распознал город. Пример: Самара, мск или Самара +200 км.')
    # A region may disambiguate a city; other conditions belong in the full search.
    if len(choices) == 1:
        remaining = remaining.replace(choices[0]['region'].casefold().replace('ё', 'е'), '')
    if remaining.strip(' ,.'):
        raise ValueError('Укажите только город и радиус: Самара +200 км.')
    return result
def distance(a,b):
    lat1,lat2 = math.radians(a['latitude']),math.radians(b['latitude'])
    delta_lat,delta_lon = lat2-lat1,math.radians(b['longitude']-a['longitude'])
    value = math.sin(delta_lat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(delta_lon/2)**2
    return 6371.0088*2*math.asin(min(1,math.sqrt(value)))


@lru_cache(maxsize=256)
def nearby_names(latitude,longitude,radius):
    origin = {'latitude':latitude,'longitude':longitude}
    return tuple(sorted({city['name'] for city in cities() if distance(origin,city)<=radius}))


def within_radius(address,origin,radius):
    choices,_ = find_city(address)
    if len(choices)!=1 or not origin:
        return False
    return distance(origin,choices[0]) <= radius