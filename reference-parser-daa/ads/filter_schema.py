"""Shared filter options and unambiguous source-attribute normalization."""
import re

FILTER_LABELS = {
    'engine':'Двигатель', 'gearbox':'Коробка', 'drive':'Привод', 'body_type':'Тип кузова',
    'color':'Цвет кузова', 'steering':'Руль', 'condition':'Состояние', 'seller':'Продавец',
}
FILTER_OPTIONS = {
    'engine':('бензин','дизель','гибрид','газ','электро'),
    'gearbox':('автомат','механика','робот','вариатор'),
    'drive':('полный','передний','задний'),
    'body_type':('седан','хетчбек','универсал','кабриолет','внедорожник','минивэн','купе','пикап','микроавтобус','лифтбек','фургон'),
    'color':('черный','белый','серый','серебристый','синий','голубой','красный','коричневый','бежевый','зеленый','желтый','оранжевый','фиолетовый','розовый'),
    'steering':('левый','правый'),
    'condition':('не битый','битый'),
    'seller':('частное лицо','автодилер'),
}
INPUT_ALIASES = {'body_type':{'хэтчбек':'хетчбек'},
                 'color':{'чёрный':'черный','серебряный':'серебристый','зелёный':'зеленый'}}


def normalize(value):
    return re.sub(r'[^\w]', '', str(value or '').casefold().replace('ё','е')).replace('_','')


ATTRIBUTE_ALIASES = {
    'condition': {'не битый':('не битый','не требует ремонта','без повреждений'),
                  'битый':('битый','требует ремонта','поврежденный','аварийный')},
    'seller': {'частное лицо':('частное лицо','частный продавец','частник','собственник'),
               'автодилер':('автодилер','официальный дилер','дилер','автосалон')},
    'gearbox': {'автомат':('автомат','акпп','at'), 'механика':('механика','механическая','мкпп','mt'),
                'робот':('робот','dct','dsg'), 'вариатор':('вариатор','cvt')},
    'drive': {'полный':('полный','4wd','awd','4x4'), 'передний':('передний','fwd','ff'), 'задний':('задний','rwd','fr')},
    'body_type': {'хетчбек':('хетчбек','хэтчбек','hatchback'), 'внедорожник':('внедорожник','джип/suv','джип','suv','кроссовер'),
                  'кабриолет':('кабриолет','родстер','открытый'), **{v:(v,) for v in FILTER_OPTIONS['body_type'] if v not in ('хетчбек','внедорожник','кабриолет')}},
    'color': {'серебристый':('серебристый','серебряный')},
    'steering': {'левый':('левый',), 'правый':('правый',)},
}


def attribute_key(field, value):
    key = normalize(value)
    if key in ('', 'unknown', 'неизвестно'): return ''
    for common, aliases in ATTRIBUTE_ALIASES.get(field, {}).items():
        if any(key.startswith(normalize(alias)) for alias in aliases):
            return normalize(common)
    return key


def matches_attribute(field, actual, selected):
    key = attribute_key(field, actual)
    if not key: return False
    expected = [attribute_key(field, value) for value in selected]
    if field in ('condition','seller','steering','gearbox','drive','body_type'):
        return key in expected
    return any(value and value in key for value in expected)
