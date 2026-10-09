"""Deterministic Russian search grammar with explicit bounds and local geography."""
import re
from decimal import Decimal, InvalidOperation
from .catalog_search import ALIASES, normalize
from .filter_values import parse_price_amount
from .geography import extract_radius, extract_location

MODEL_ALIASES = {
    'camry':('камри','камриха'), 'solaris':('солярис',), 'corolla':('королла',),
    'focus':('фокус',), 'polo':('поло',), 'octavia':('октавия',), 'rapid':('рапид',),
    'granta':('гранта',), 'vesta':('веста',), 'priora':('приора',), 'kalina':('калина',),
    'logan':('логан',), 'duster':('дастер',), 'sportage':('спортейдж','спортаж'),
    'rio':('рио',), 'creta':('крета',), 'tucson':('туссан',), 'tiguan':('тигуан',),
    'sx11':('кулрей','кулрэй','coolray'), 'coolray':('кулрей','кулрэй'), 'atlas':('атлас',), 'monjaro':('монжаро',),
    'jolion':('джолион','жолион'), '3series':('3 серия','тройка'), '5series':('5 серия','пятерка'),
    'eclass':('е класс','e класс','е-класс'), 'cclass':('с класс','c класс','с-класс'),
    'sclass':('s класс','s-класс'), 'x5':('х5','икс5'), 'x3':('х3','икс3'),
}
MAKE_EXTRA = {'mercedesbenz':('мерс','мерседес'),'bmw':('бэха','беха','бимер')}
AMOUNT = r'\d+(?:[., \u00a0\u202f]\d+)*\s*(?:млн\.?|миллион\w*|тыс\.?|тысяч\w*|[мк](?!\w))?\s*(?:₽|руб\w*\.?)?'


def money(value):
    value = value.strip().casefold().replace('\u00a0',' ').replace('\u202f',' ')
    match = re.search(r'(млн|миллион\w*|тыс|тысяч\w*|[мк])(?=\W|$)',value)
    if not match:
        return parse_price_amount(value)
    number = value[:match.start()].strip().replace(' ','').replace(',','.')
    try:
        amount = Decimal(number)*(1_000_000 if match[1].startswith('м') else 1000)
    except InvalidOperation:
        raise ValueError('Не понял сумму. Пример: до 1.2 млн или от 500 000.') from None
    if amount < 0 or amount != amount.to_integral_value() or amount > 2_147_483_647:
        raise ValueError('Укажите сумму в целых рублях до 2 млрд.')
    return int(amount)


def make_aliases(make):
    key = normalize(make.slug)
    return {make.name,make.slug,*ALIASES.get(key,()),*MAKE_EXTRA.get(key,())}


def model_aliases(model):
    return {model.name,model.slug,*MODEL_ALIASES.get(normalize(model.slug),())}


def leading(text,aliases):
    for alias in sorted(aliases,key=len,reverse=True):
        pattern = r'[\s-]*'.join(re.escape(s) for s in re.split(r'[\s_-]+',alias) if s)
        match = re.match(pattern+r'(?=\W|$)',text,re.I)
        if match:
            return match
    return None


def parse_request(text,makes):
    if len(text)>300:
        raise ValueError('Запрос слишком длинный. Укажите автомобиль и условия одной строкой.')
    text = re.sub(r'[–—]','-',text.strip().casefold().replace('ё','е'))
    make = None
    for node in makes:
        match = leading(text,make_aliases(node))
        if match:
            make,text = node,text[match.end():].strip()
            break
    parsed = {'make':make,'year':'','price':'','city':[],'location':{},'radius_km':0,'city_choices':[],
              'engine':[],'gearbox':[],'drive':[],'body_type':[],'mileage':'','owners':'','color':[],'steering':[],'condition':[],'seller':[],'platform':[]}
    text, parsed['radius_km'] = extract_radius(text)
    # Extract ownership/mileage before budget bounds so «до 2 владельцев»
    # and «пробег от 100 тыс км» are not interpreted as rubles.
    owner = re.search(r'\b(?:от\s*)?(\d{1,3})\s*(?:-|до)\s*(\d{1,3})\s*(?:владельц(?:ев|а)|владелец)\b', text)
    if owner:
        lo,hi=map(int,owner.groups())
        if not 0 <= lo <= hi <= 99: raise ValueError('Укажите от 0 до 99 владельцев, начальное число не больше конечного.')
        parsed['owners']=f'{lo}-{hi}'
    else:
        owner=re.search(r'(?<!\w)(?:(от|до)\s*)?(\d{1,3})\s*(?:владельц(?:ев|а)|владелец)\b',text)
        if owner:
            amount=int(owner[2])
            if amount>99: raise ValueError('Количество владельцев должно быть от 0 до 99.')
            parsed['owners']=f'{amount}-' if owner[1]=='от' else f'-{amount}' if owner[1]=='до' else f'{amount}-{amount}'
    if owner: text=text[:owner.start()]+' '+text[owner.end():]
    mileage=re.search(r'\bпробег\s*(?:от\s*)?('+AMOUNT+r')\s*(?:-|до)\s*('+AMOUNT+r')\s*км\b',text)
    if mileage:
        lo,hi=(money(v) for v in mileage.groups())
        if not 0 <= lo <= hi <= 10_000_000: raise ValueError('Проверьте диапазон пробега: от меньшего к большему, до 10 млн км.')
        parsed['mileage']=f'{lo}-{hi}'
    else:
        mileage=re.search(r'\bпробег\s*(?:(от|до)\s*)?('+AMOUNT+r')\s*км\b',text)
        if mileage:
            amount=money(mileage[2])
            if amount>10_000_000: raise ValueError('Пробег должен быть не больше 10 млн км.')
            parsed['mileage']=f'{amount}-' if mileage[1]=='от' else '-'+str(amount)
    if mileage: text=text[:mileage.start()]+' '+text[mileage.end():]
    # Four digit years with an adjacent bound are years; sums use руб./тыс./млн.
    year_bounds = {}
    year_pattern = r'(?<!\w)(от|до)\s*((?:19|20)\d{2})(?![\d.,]|\s*\d|\s*(?:млн|тыс|руб|₽))\b'
    for match in list(re.finditer(year_pattern,text))[::-1]:
        if match[1] in year_bounds: raise ValueError('Повторяется граница года выпуска.')
        year_bounds[match[1]] = int(match[2])
        text = text[:match.start()]+' '+text[match.end():]
    match = re.search(r'\b((?:19|20)\d{2})\s*-\s*((?:19|20)\d{2})\b',text)
    if year_bounds:
        lo,hi = year_bounds.get('от'),year_bounds.get('до')
        if lo is not None and hi is not None and lo>hi: raise ValueError('Начальный год больше конечного.')
        parsed['year'] = f'{lo if lo is not None else ""}-{hi if hi is not None else ""}'
        if match: raise ValueError('Укажите только один диапазон лет.')
    elif match:
        start,end = map(int,match.groups())
        if start>end: raise ValueError('Начальный год не может быть больше конечного.')
        parsed['year'] = f'{start}-{end}'
        text = text[:match.start()]+' '+text[match.end():]
    # Extract money before bare years so 2 000 000 and до 2015 руб. stay prices.
    prices = {}
    for match in list(re.finditer(r'(?<!\w)(от|до)\s*('+AMOUNT+r')',text))[::-1]:
        key,value = match[1],money(match[2])
        if key in prices: raise ValueError('Укажите только одну цену «от» и одну «до».')
        prices[key] = value
        text = text[:match.start()]+' '+text[match.end():]
    if prices:
        lo,hi = prices.get('от'),prices.get('до')
        if lo is not None and hi is not None and lo>hi: raise ValueError('Цена «от» больше цены «до».')
        parsed['price'] = f'{lo if lo is not None else ""}-{hi if hi is not None else ""}'
    years = re.findall(r'\b(?:19|20)\d{2}\b',text)
    if years:
        if len(years)>1 or parsed['year']: raise ValueError('Укажите один диапазон лет: 2018-2020.')
        parsed['year'] = f'{years[0]}-{years[0]}'
        text = re.sub(r'\b'+years[0]+r'\b',' ',text)
    location, text = extract_location(text, parsed['radius_km'])
    parsed.update(location)
    vocab = {'engine':{'дизель':r'дизель\w*','бензин':r'бензин\w*','гибрид':r'гибрид\w*','электро':r'электро\w*','газ':r'газ|на газу'},
             'gearbox':{'автомат':r'акпп|автомат\w*|ат','механика':r'мкпп|механик\w*|мт','вариатор':r'вариатор\w*|cvt','робот':r'робот\w*|dct'},
             'drive':{'полный':r'полный(?: привод)?|4x4|4х4','передний':r'передний(?: привод)?','задний':r'задний(?: привод)?'},
             'body_type':{'седан':r'седан','универсал':r'универсал','хэтчбек':r'хэтчбек|хетчбек','внедорожник':r'внедорожник|кроссовер','лифтбек':r'лифтбек','купе':r'купе','кабриолет':r'кабриолет|родстер','минивэн':r'минивэн|минивен','пикап':r'пикап','микроавтобус':r'микроавтобус','фургон':r'фургон'},
             'steering':{'левый':r'левый руль','правый':r'правый руль'},
             'condition':{'не битый':r'не\s+битый|не\s+битая|не требует ремонта','битый':r'битый|битая|требует ремонта'},
             'seller':{'частное лицо':r'частное лицо|частник|от собственника','автодилер':r'автодилер|автосалон|(?:от )?дилер(?:а)?'},
             'color':{'черный':r'черный|черная','белый':r'белый|белая','серый':r'серый|серая','серебристый':r'серебристый|серебряный',
                      'синий':r'синий|синяя','голубой':r'голубой|голубая','красный':r'красный|красная','зеленый':r'зеленый|зеленая',
                      'коричневый':r'коричневый','бежевый':r'бежевый','желтый':r'желтый','оранжевый':r'оранжевый','фиолетовый':r'фиолетовый','розовый':r'розовый'},
             'platform':{'avito':r'авито|avito','autoru':r'авто\.?ру|auto\.ru','drom':r'дром|drom'}}
    for field,values in vocab.items():
        for value,pattern in values.items():
            regex = r'(?<!\w)(?:'+pattern+r')(?!\w)'
            if re.search(regex,text,re.I):
                parsed[field].append(value)
                text = re.sub(regex,' ',text,flags=re.I)
    text = re.sub(r'\b(?:в|года|год)\b',' ',text)
    translation = str.maketrans({'с':'c','е':'e','х':'x','а':'a','м':'m','г':'g','ф':'f','в':'v'})
    text = ' '.join(word.translate(translation) if re.fullmatch(r'[a-zсехамгфв]*\d+[a-zсехамгфв\d]*',word) else word for word in text.split())
    parsed['model_text'] = text.strip(' ,')
    return parsed
