"""Explicit Lexus badge names shared by catalogue and source adapters."""
import re

_FAMILIES = r'IS|ES|GS|LS|HS|CT|UX|NX|RX|GX|LX|RC|LC|LM|RZ|SC|TX'
_BADGE = r'\d{3}\s*(?:h(?:\s*(?:l|plus|\+))?|[dtec]|l)?'


def lexus_badge(value):
    match = re.fullmatch(r'(' + _FAMILIES + r')[ _-]*(' + _BADGE + r')', str(value), re.I)
    return (match[1].lower(), re.sub(r'\s+', '', match[2]).lower().replace('+','plus')) if match else None


def lexus_model_slug(ad):
    """Read an explicit badge; IS 2.5 petrol is marketed as IS250 by Drom."""
    if re.sub(r'\W', '', ad.get('brand', '')).casefold() != 'lexus':
        return ''
    title = ad.get('name', '')
    # Structured modification identifies the badge even when the headline says RX/NX/LX.
    for value in (ad.get('source_modification', ''), title):
        match = re.match(r'^(?:Lexus\s+)?(' + _FAMILIES + r')[ _-]*(' + _BADGE + r')(?=\W|$)', value, re.I)
        if match:
            slug = re.sub(r'\s+', '', match[1]+match[2]).lower().replace('+','plus')
            # Avito calls the turbo NX "NX 200 2.0t"; Drom distinguishes NX200t.
            if slug == 'nx200' and re.match(r'\s+2[.,]0\s*t\b', value[match.end():], re.I):
                return 'nx200t'
            return slug
    raw = re.sub(r'\W', '', ad.get('model', '')).casefold()
    if raw != 'is':
        return ''
    engine = ad.get('engine') or ''
    if re.search(r'гибрид|hybrid', engine+' '+title, re.I):
        return ''
    title_engine = re.match(r'^Lexus\s+IS\s+2[.,]5\s+(?:AT|MT)\b', title, re.I)
    petrol_engine = re.search(r'бензин|petrol|gasoline', engine, re.I) and re.search(r'(?<!\d)2[.,]5\s*л', engine, re.I)
    if (title_engine or petrol_engine) and not re.search(r'кабриолет|купе|convertible|coupe', ad.get('body_type') or '', re.I):
        return 'is250'
    return ''
# Official Auto.ru sections group these explicitly numbered Infiniti badges.
INFINITI_FAMILIES={'ex':'ex','fx':'fx','g':'g35','i':'i','jx':'jx','m':'m','q':'q'}


def infiniti_badge(value):
    found=re.fullmatch(r'(EX|FX|G|I|JX|M|Q)[ _-]*(\d{2}(?:d|h)?)(?:[ _-]*(hybrid))?',str(value),re.I)
    if not found:return None
    number=found[2].lower()
    return found[1].lower(),number[:-1] if number.endswith('h') else number,bool(found[3]) or number.endswith('h')


def explicit_family_model(ad):
    """Resolve a source family only when the title explicitly names its badge/body."""
    brand=re.sub(r'\W','',ad.get('brand','')).casefold()
    raw=re.sub(r'\W','',ad.get('model','')).casefold()
    if brand=='infiniti':
        found=re.match(r'^Infiniti\s+((?:EX|FX|G|I|JX|M|Q)[ _-]*\d{2}(?:d|h)?(?:[ _-]*Hybrid)?)(?=\W|$)',ad.get('name',''),re.I)
        badge=infiniti_badge(found[1]) if found else None
        if badge:
            family,number,hybrid=badge
            slug=family+number+('_hybrid' if hybrid else '')
            allowed={family,re.sub(r'\W','',slug)}
            if ad.get('platform')=='autoru':allowed.add(INFINITI_FAMILIES[family])
            if raw in allowed:return 'infiniti/'+slug
    if brand=='bmw':
        found=re.match(r'^BMW\s+([356])(?:[-_ ]Series|\s+сери[яи])\s+Gran\s+Turismo\b',ad.get('name',''),re.I)
        if found and raw in (found[1]+'er',found[1]+'series',found[1]+'seriesgranturismo'):
            return 'bmw/'+found[1]+'-series_gran_turismo'
    return ''

def lexus_candidate_score(ad, model_slug):
    """Cheap audit ordering only; a matching explicit card badge is still required."""
    badge = lexus_badge(model_slug)
    if not badge:return 0
    family, number = badge
    title = ad.get('name', '')
    volume = int(number[:3]) / 100
    engine = re.match(r'^Lexus\s+' + family + r'\s+(\d[.,]\d)\s+(AT|MT|CVT)\b', title, re.I)
    if not engine:return 0
    score = 2 if float(engine[1].replace(',', '.')) == volume else 0
    transmission = 'CVT' if 'h' in number or model_slug == 'nx200' else 'AT'
    return score + int(engine[2].upper() == transmission)
