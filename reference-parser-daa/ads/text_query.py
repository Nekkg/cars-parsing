import re
from .catalog_search import ALIASES, normalize, ranked_rows


def split_vehicle_query(text, makes):
    """Resolve a leading make; leave model/modification matching to its catalogue."""
    text=text.strip()
    words=text.split()
    for size in range(min(3,len(words)),0,-1):
        prefix=normalize(' '.join(words[:size]))
        exact=[m for m in makes if prefix in {normalize(m.name),normalize(m.slug),*(normalize(a) for a in ALIASES.get(normalize(m.slug),()))}]
        if exact:
            make=exact[0]
            remainder=' '.join(words[size:])
            years=re.findall(r'\b(?:19|20)\d{2}\b',remainder)
            if len(years)>1: raise ValueError('Укажите один год выпуска, например BMW 3 2018.')
            year=years[0] if years else ''
            remainder=re.sub(r'\b(?:19|20)\d{2}\b','',remainder).strip()
            remainder=re.sub(r'\b(?:года|год|г\.)\b','',remainder,flags=re.I).strip()
            return make,remainder,year
    return None


def model_suggestions(make, text, models):
    from .query_language import model_aliases
    exact_alias=[model for model in models if normalize(text) in {normalize(alias) for alias in model_aliases(model)}]
    if exact_alias: return exact_alias,''
    query=normalize(text)
    variant=''
    if normalize(make.slug)=='bmw':
        match=re.fullmatch(r'm?([1-8])\d{2}(?:[ide]|li|d)?',query)
        if match:
            variant=text.upper()
            query=match[1]+'series'
        elif re.fullmatch(r'[1-8]',query): query+='series'
    if normalize(make.slug)=='mercedesbenz':
        match=re.fullmatch(r'([ceas]|gl[aceks]|ml|cl[aks]?)(\d{3})(?:[diel]*)',query)
        if match:
            variant=match[1].upper()+match[2]
            query=match[1]+'class' if match[1] in ('c','e','a','s') else match[1]
    ranked=ranked_rows(models,query,make_slug=make.slug)
    exact=[m for m in ranked if query in {normalize(m.name),normalize(m.slug)}]
    return exact or ranked[:8],variant
