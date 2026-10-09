import re


def parse_price_amount(value):
    """Whole rubles with optional thousands separators, never guess decimals."""
    value = str(value).strip().casefold()
    value = re.sub(r'\s*(?:₽|руб\.?|рублей|р\.)$', '', value).strip()
    value = re.sub(r'\s+', ' ', value)
    if not re.fullmatch(r'[0-9]+|[0-9]{1,3}([ .,])[0-9]{3}(?:\1[0-9]{3})*', value):
        raise ValueError('Введите сумму в целых рублях: 500000, 500 000 или 500.000. Для удаления границы — «нет».')
    digits = re.sub(r'[ .,]', '', value)
    if len(digits) > 10:
        raise ValueError('Сумма слишком большая. Введите не более 10 цифр.')
    return int(digits)


def bounds(value):
    """Open bounds; a legacy single number means an upper budget."""
    if not value or value == 'unknown':
        return None, None
    value = re.sub(r'[\s\u00a0₽]', '', str(value)).replace('–', '-').replace('—', '-')
    if value.isdigit():
        return None, int(value)
    parts = value.split('-')
    if len(parts) != 2 or any(p and not p.isdigit() for p in parts):
        raise ValueError('Укажите неотрицательные числа.')
    lo, hi = (int(p) if p else None for p in parts)
    if lo is not None and hi is not None and lo > hi:
        raise ValueError('Цена «от» не может быть больше цены «до».')
    return lo, hi


def range_label(value):
    lo, hi = bounds(value)
    return ' · '.join(f'{label} {n:,}'.replace(',', ' ') for label, n in [('от',lo),('до',hi)] if n is not None) or 'без ограничений'


def describe(filters):
    vehicle = filters.get('vehicle', {})
    parts = [vehicle[k]['name'] for k in ('make','model') if k in vehicle]
    if filters.get('variant'): parts.append(filters['variant'])
    if filters.get('generation_hint'): parts.append('('+filters['generation_hint']+')')
    from .vehicle_selection import selected_generations
    generations = selected_generations(filters)
    if generations:
        labels = []
        for gen in generations:
            label = gen.get('description') or gen['name']
            if gen.get('codes'):
                label += ' (' + ', '.join(gen['codes']) + ')'
            labels.append(label)
        parts.append('; '.join(labels))
    lines = ['🚗 ' + (' '.join(parts) or ', '.join(filters.get('brand') or []) or 'Любой автомобиль')]
    labels = {'price':'Цена, ₽','year':'Год','city':'Город','platform':'Площадки',
              'mileage':'Пробег, км','engine':'Двигатель','gearbox':'Коробка','owners':'Владельцы',
              'body_type':'Кузов','color':'Цвет','drive':'Привод','steering':'Руль',
              'condition':'Состояние','seller':'Продавец'}
    for key, label in labels.items():
        value = filters.get(key)
        if not value or value == 'unknown' or value == ['unknown']:
            continue
        if key in ('price','year','mileage','owners'):
            try: value = range_label(value)
            except ValueError: value = str(value) + ' (проверьте формат)'
        elif isinstance(value, list):
            value = ', '.join(value)
        lines.append(f'{label}: {value}')
    if filters.get('radius_km'): lines.append('Радиус: +'+str(filters['radius_km'])+' км')
    return '\n'.join(lines)
