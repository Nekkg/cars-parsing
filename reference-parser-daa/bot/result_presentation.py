"""Compact, escaped presentation for Telegram's card and list limits."""
from html import escape
from urllib.parse import urlparse
from ads.filter_values import describe
from ads.search_status import SOURCE_NAMES
from aiogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard


def amount(value):
    return f'{int(value):,}'.replace(',',' ') if value is not None else '—'


def url(value):
    try: parsed = urlparse(str(value or ''))
    except ValueError: return ''
    return str(value) if parsed.scheme in ('https','http') and parsed.hostname and parsed.username is None else ''


def pictures(ad):
    from parsers.photos import unique_photos, DOMAINS
    if ad.platform not in DOMAINS: return []
    return unique_photos(getattr(ad,'images',[]),ad.platform) or unique_photos([ad.image],ad.platform)


def clean(value,limit):
    text=str(value or '—').encode('utf-16-le')[:limit*2].decode('utf-16-le',errors='ignore')
    return escape(text)


def caption(ad,photo_available=True,filters=None):
    parts = [f'<b>{clean(ad.name,145)}</b>',f'💰 <b>{amount(ad.price)} ₽</b> · {clean(ad.year,10)}',
             f'📍 {clean(ad.city,65)} · {SOURCE_NAMES.get(ad.platform,clean(ad.platform,20))}']
    if ad.mileage is not None: parts.append('🛣 Пробег: '+amount(ad.mileage)+' км')
    specs = [getattr(ad,key,None) for key in ('engine','gearbox','body_type','drive')]
    specs = [str(value) for value in specs if value and value!='unknown']
    if specs: parts.append('⚙️ '+clean(', '.join(specs),165))
    if ad.generation and (getattr(ad,'generation_evidence',{}) or {}).get('status','resolved')=='resolved':
        parts.append('🚗 '+clean(ad.generation,130))
    link = url(ad.link)
    if link: parts.append(f'<a href="{escape(link,quote=True)}">Открыть объявление</a>')
    if not photo_available: parts.append('📷 Фото сейчас недоступно. Откройте объявление по ссылке.')
    return '\n'.join(parts)


def code(view,action,arg=''):
    return f'rv:{view.pk.hex}:{action}'+(f':{arg}' if arg!='' else '')


def keyboard(view,counts,total,photo_count=0,following=False,list_mode=False,advanced=False):
    b = lambda text,action,arg='': Button(text=text,callback_data=code(view,action,arg))
    rows = []
    if list_mode:
        pages = max(1,(total+9)//10)
        rows.append([b('⬅️','listpage',max(0,view.list_page-1)),b(f'{view.list_page+1}/{pages}','noop'),
                     b('➡️','listpage',min(pages-1,view.list_page+1))])
    else:
        rows.append([b('⬅️','move',-1),b(f'{view.position+1 if total else 0} / {total}','noop'),b('➡️','move',1)])
    rows.append([b(('• ' if view.platform=='all' else '')+f'Все {sum(counts.values())}','source','all'),
                 *[b(('• ' if view.platform==source else '')+f'{label} {counts.get(source,0)}','source',source)
                   for source,label in SOURCE_NAMES.items()]])
    if not list_mode:
        rows.append([b('◀️','photo',-1),b(f'📷 Фото {min(view.photo_index+1,photo_count) if photo_count else 0} / {photo_count}','gallery'),b('▶️','photo',1)])
    row = [b('📷 Карточкой' if list_mode else '📋 Списком','card' if list_mode else 'list'),
           b('✅ Слежу' if following else '🔔 Следить','follow')]
    rows.append(row)
    if advanced:
        rows.append([b(('✓ ' if view.sort_order==order else '')+label,'sort',order)
                     for order,label in [('newest','Новые'),('cheap','Дешевле'),('expensive','Дороже')]])
    row = [b('🔄 Обновить','refresh')]
    if advanced: row.append(b('Сбор','status'))
    rows.append(row)
    rows.append([b('🔎 Новый поиск','new')])
    return Keyboard(inline_keyboard=rows)


def listing(view,ads,total):
    title = describe(view.filters).replace('\n',' · ').replace('🚗 ','')[:155]
    start = view.list_page*10
    lines = [f'<b>📋 {escape(title)}</b>',f'{start+1 if total else 0}–{min(start+len(ads),total)} из {total} · {SOURCE_NAMES.get(view.platform,"Все площадки")}']
    for i,ad in enumerate(ads,start+1):
        link = url(ad.link)
        name = clean(ad.name,100)
        lines.append(f'{i}. '+(f'<a href="{escape(link,quote=True)}">{name}</a>' if link else name))
        lines.append(f'   {amount(ad.price)} ₽ · {clean(ad.year,8)} · {amount(ad.mileage)} км · {clean(ad.city,45)} · {SOURCE_NAMES.get(ad.platform,ad.platform)}')
    if not total: lines.append('Подходящих объявлений пока нет. Обновите выдачу после сбора.')
    return '\n'.join(lines)