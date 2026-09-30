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


def numeric_params(filters, source):
    params={}
    mapping = {'drom':{'price':('minprice','maxprice'),'year':('minyear','maxyear')},
               'autoru':{'price':('price_from','price_to'),'year':('year_from','year_to')},
               'avito':{'price':('pmin','pmax')}}[source]
    for field, names in mapping.items():
        lo,hi=bounds(filters.get(field))
        gen=filters.get('vehicle',{}).get('generation',{})
        if field == 'year':
            if gen.get('year_from'): lo=max(lo or 0,gen['year_from'])
            if gen.get('year_to'): hi=min(hi or 9999,gen['year_to'])
        for name,value in zip(names,(lo,hi)):
            if value is not None: params[name]=value
    return params
