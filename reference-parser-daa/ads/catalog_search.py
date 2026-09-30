"""Editable display priorities, not a ranking of sales or catalogue exclusions."""
import re
from difflib import SequenceMatcher

POPULAR = ('bmw', 'audi', 'mercedesbenz', 'toyota', 'volkswagen', 'lada',
           'kia', 'hyundai', 'nissan', 'honda', 'lexus', 'skoda', 'ford',
           'renault', 'mitsubishi', 'mazda', 'chevrolet', 'haval', 'chery',
           'geely', 'changan', 'omoda', 'exeed', 'tank', 'volvo', 'porsche',
           'landrover', 'peugeot', 'citroen', 'opel', 'subaru', 'suzuki',
           'uaz', 'gaz', 'moskvich')
ALIASES = {
    'bmw': ('бмв', 'бэ эм вэ'), 'audi': ('ауди',),
    'mercedesbenz': ('мерседес', 'мерс', 'mercedes'),
    'volkswagen': ('фольксваген', 'фольц', 'vw'), 'toyota': ('тойота', 'тоёта'),
    'lada': ('лада', 'ваз', 'vaz'), 'kia': ('киа', 'кия'),
    'hyundai': ('хендай', 'хундай', 'хёндэ'), 'nissan': ('ниссан',),
    'honda': ('хонда',), 'lexus': ('лексус',), 'skoda': ('шкода',),
    'ford': ('форд',), 'renault': ('рено',), 'mitsubishi': ('митсубиси', 'мицубиси'),
    'mazda': ('мазда',), 'chevrolet': ('шевроле',), 'haval': ('хавал', 'хавейл'),
    'chery': ('чери',), 'geely': ('джили',), 'changan': ('чанган',),
    'omoda': ('омода',), 'exeed': ('эксид',), 'tank': ('танк',),
    'volvo': ('вольво',), 'porsche': ('порше',), 'landrover': ('лендровер', 'лэндровер'),
    'peugeot': ('пежо',), 'citroen': ('ситроен',), 'opel': ('опель',),
    'subaru': ('субару',), 'suzuki': ('сузуки',), 'uaz': ('уаз',),
    'gaz': ('газ',), 'moskvich': ('москвич',),
}

# Editable UI priorities, intentionally not advertised as measured sales ranks.
MODEL_PRIORITIES = {
    'bmw': ('3series','5series','x5','x3','x6','1series','7series','x1','4series'),
    'audi': ('a4','a6','q5','q7','a3','q3','a5','a8'),
    'mercedesbenz': ('eclass','cclass','sclass','glc','gle','glk','mclass','gclass'),
    'toyota': ('camry','corolla','rav4','landcruiserprado','landcruiser','markii','vitz'),
    'volkswagen': ('polo','tiguan','passat','golf','touareg','jetta'),
    'lada': ('granta','vesta','priora','kalina','largus','niva','2114','2110'),
    'kia': ('rio','sportage','ceed','optima','sorento','cerato','k5'),
    'hyundai': ('solaris','creta','santafe','tucson','elantra','sonata','getz'),
    'nissan': ('qashqai','xtrail','almera','teana','juke','tiida'),
    'honda': ('civic','accord','crv','fit','freed'),
    'lexus': ('rx','nx','lx','es','gs','is'), 'skoda': ('octavia','rapid','superb','kodiaq','yeti'),
    'ford': ('focus','mondeo','kuga','fusion','fiesta'), 'renault': ('logan','duster','sandero','kaptur','megane'),
    'mitsubishi': ('outlander','lancer','asx','pajero','pajerosport'),
    'mazda': ('mazda3','mazda6','cx5','3','6','demio'),
    'chevrolet': ('niva','cruze','lacetti','aveo','captiva'),
    'haval': ('jolion','f7','f7x','dargo','h9'), 'chery': ('tiggo7promax','tiggo7pro','tiggo4pro','tiggo8pro'),
    'geely': ('coolray','monjaro','atlas','emgrand','tugella'),
    'changan': ('cs55plus','cs35plus','uni k','uni v'),
    'volvo': ('xc90','xc60','s60','s80'), 'porsche': ('cayenne','macan','panamera','911'),
    'landrover': ('rangerover','rangeroversport','discovery','freelander'),
    'opel': ('astra','corsa','insignia','zafira'), 'subaru': ('forester','outback','impreza','legacy'),
    'suzuki': ('grandvitara','sx4','vitara','swift'), 'uaz': ('patriot','hunter'),
}


def normalize(value):
    return re.sub(r'[^\w]', '', value.casefold().replace('ё', 'е')).replace('_', '')


def ranked_rows(rows, query='', popular_only=False, make_slug=''):
    query = normalize(query)
    priorities = {name: index for index, name in enumerate(POPULAR)}
    scored = []
    for row in rows:
        key = normalize(row.slug)
        names = {normalize(row.name), key}
        if row.kind == 'make':
            names.update(normalize(alias) for alias in ALIASES.get(key, ()))
        priority = priorities.get(key, len(POPULAR)) if row.kind == 'make' else len(POPULAR)
        if row.kind == 'model':
            models = [normalize(v) for v in MODEL_PRIORITIES.get(normalize(make_slug), ())]
            priority = models.index(key) if key in models else len(models)
        if row.kind == 'generation':
            priority = (row.data.get('year_from') or 0) * 10000 + (row.data.get('year_to') or 9999)
        if not query and popular_only and priority == len(POPULAR):
            continue
        score = 0
        if query:
            for name in names:
                candidate = (4 if query == name else 3 if name.startswith(query)
                             else 2 if query in name else 0)
                if not candidate and len(query) >= 3:
                    ratio = SequenceMatcher(None, query, name).ratio()
                    candidate = ratio if ratio >= .72 else 0
                score = max(score, candidate)
            if not score:
                continue
        scored.append((-score, priority, row.name.casefold(), row.pk, row))
    return [item[-1] for item in sorted(scored)]
