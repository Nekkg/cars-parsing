"""Validated web inputs; no whole-advertisement-table choice queries."""
from types import SimpleNamespace
from datetime import date
from django import forms
from .models import CatalogNode
from .geography import cities, parse_city_input
from .query_language import money
from .search import snapshot
from .filter_schema import FILTER_OPTIONS, FILTER_LABELS, INPUT_ALIASES

SOURCE_CHOICES = [('avito', 'Avito'), ('autoru', 'Auto.ru'), ('drom', 'Дром')]
FILTER_CHOICES = {field:[(v,v.capitalize()) for v in values] for field,values in FILTER_OPTIONS.items()}


class WebSearchForm(forms.Form):
    make_id = forms.IntegerField(required=False, min_value=1)
    model_id = forms.IntegerField(required=False, min_value=1)
    generation_ids = forms.Field(required=False)
    generation_hint = forms.CharField(required=False, max_length=32)
    variant = forms.CharField(required=False, max_length=60)
    city = forms.CharField(required=False, max_length=150)
    city_id = forms.CharField(required=False, max_length=50)
    radius_km = forms.IntegerField(required=False, min_value=0, max_value=1000)
    price_min = forms.CharField(required=False, max_length=30)
    price_max = forms.CharField(required=False, max_length=30)
    year_min = forms.IntegerField(required=False, min_value=1900, max_value=date.today().year+1)
    year_max = forms.IntegerField(required=False, min_value=1900, max_value=date.today().year+1)
    mileage_min = forms.CharField(required=False, max_length=30)
    mileage_max = forms.CharField(required=False, max_length=30)
    owners_min = forms.IntegerField(required=False, min_value=0, max_value=99)
    owners_max = forms.IntegerField(required=False, min_value=0, max_value=99)
    color_custom = forms.CharField(required=False, max_length=150)
    platform = forms.MultipleChoiceField(required=False, choices=SOURCE_CHOICES)
    engine = forms.MultipleChoiceField(required=False, choices=FILTER_CHOICES['engine'])
    gearbox = forms.MultipleChoiceField(required=False, choices=FILTER_CHOICES['gearbox'])
    drive = forms.MultipleChoiceField(required=False, choices=FILTER_CHOICES['drive'])
    body_type = forms.MultipleChoiceField(required=False, choices=FILTER_CHOICES['body_type'])
    color = forms.MultipleChoiceField(required=False, choices=FILTER_CHOICES['color'])
    steering = forms.MultipleChoiceField(required=False, choices=FILTER_CHOICES['steering'])
    condition = forms.MultipleChoiceField(required=False, choices=FILTER_CHOICES['condition'])
    seller = forms.MultipleChoiceField(required=False, choices=FILTER_CHOICES['seller'])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field, aliases in INPUT_ALIASES.items():
            self.fields[field].choices = [*self.fields[field].choices, *((v,v) for v in aliases)]

    def clean_generation_ids(self):
        values = self.cleaned_data['generation_ids'] or []
        if not isinstance(values, list) or len(values) > 30:
            raise forms.ValidationError('Выберите поколения из списка.')
        try:
            if any(type(v) not in (int, str) or not str(v).isdigit() or int(v) < 1 for v in values):
                raise ValueError
            return sorted({int(v) for v in values})
        except (TypeError, ValueError):
            raise forms.ValidationError('Выберите поколения из списка.') from None

    def clean(self):
        values = super().clean()
        make = CatalogNode.objects.filter(pk=values.get('make_id'), kind='make').first()
        model = CatalogNode.objects.filter(pk=values.get('model_id'), kind='model').select_related('parent').first()
        if values.get('make_id') and not make:
            self.add_error('make_id', 'Марка отсутствует в каталоге.')
        if values.get('model_id') and (not model or not make or model.parent_id != make.pk):
            self.add_error('model_id', 'Выберите модель выбранной марки.')
        canonical_model = model
        if model and model.data.get('canonical_model_key'):
            canonical_model = CatalogNode.objects.filter(kind='model', key=model.data['canonical_model_key']).first() or model
        generations = values.get('generation_ids', [])
        nodes = list(CatalogNode.objects.filter(pk__in=generations, kind='generation', parent=canonical_model)) if canonical_model else []
        if generations and len(nodes) != len(generations):
            self.add_error('generation_ids', 'Поколения должны принадлежать выбранной модели.')
        if values.get('generation_hint'):
            import re
            if not re.fullmatch(r'[A-Za-zА-Яа-я0-9 -]+', values['generation_hint']) or not model:
                self.add_error('generation_hint', 'Код кузова требует выбранной модели.')
        location, radius = {}, values.get('radius_km') or 0
        try:
            if values.get('city_id'):
                location = next((c for c in cities() if c['id'] == values['city_id']), {})
                if not location:
                    raise ValueError('Выберите город из подсказок.')
            elif values.get('city'):
                parsed = parse_city_input(values['city'])
                if parsed['city_choices']:
                    raise ValueError('Есть несколько городов с таким названием. Выберите регион в подсказках.')
                location = parsed['location']
                radius = parsed['radius_km'] or radius
            elif radius:
                raise ValueError('Для радиуса сначала выберите город.')
        except ValueError as error:
            self.add_error('city', str(error))
        ranges = {}
        for field in ('price', 'year', 'mileage', 'owners'):
            ends = []
            for suffix in ('min', 'max'):
                key = field+'_'+suffix
                value = values.get(key)
                try:
                    value = money(value) if field in ('price','mileage') and value else value
                    if field == 'mileage' and value not in (None, '') and value > 10_000_000:
                        raise ValueError('Пробег должен быть не больше 10 млн км.')
                    if value is not None and value != '' and value > 2_147_483_647:
                        raise ValueError('Сумма должна быть не больше 2 млрд рублей.')
                except ValueError as error:
                    self.add_error(key, str(error)); value = None
                ends.append(value if value != '' else None)
            lo, hi = ends
            if lo is not None and hi is not None and lo > hi:
                self.add_error(field+'_max', 'Значение «до» должно быть не меньше значения «от».')
            ranges[field] = f'{lo if lo is not None else ""}-{hi if hi is not None else ""}' if lo is not None or hi is not None else None
        custom_colors = []
        if values.get('color_custom'):
            import re
            custom_colors = [v.strip().casefold().replace('ё','е') for v in values['color_custom'].split(',') if v.strip()]
            if len(custom_colors)>8 or any(not re.fullmatch(r'[a-zа-я -]{2,40}',v) for v in custom_colors):
                self.add_error('color_custom', 'Укажите цвет словами, несколько цветов — через запятую.')
        if self.errors:
            return values
        defaults = {key: [] for key in ('platform','brand','engine','gearbox','condition','seller','city','body_type','color','drive','steering')}
        defaults.update(price=ranges['price'], year=ranges['year'], mileage=ranges['mileage'], owners=ranges['owners'],
                        variant=values.get('variant',''), generation_hint=values.get('generation_hint',''),
                        radius_km=radius, location=location, vehicle=canonical_model or make,
                        vehicle_id=(canonical_model or make).pk if canonical_model or make else None,
                        generation_keys=[node.key for node in nodes])
        for field in ('platform', *FILTER_OPTIONS):
            defaults[field] = sorted({INPUT_ALIASES.get(field, {}).get(v,v) for v in values.get(field) or []})
        defaults['color'] = sorted(set(defaults['color']+custom_colors))
        defaults['brand'] = [make.name.lower()] if make else []
        defaults['city'] = [location['name']] if location else []
        self.filters = snapshot(SimpleNamespace(**defaults))
        # The existing collector cannot perform an unbounded all-makes search efficiently.
        if not make:
            self.add_error('make_id', 'Выберите марку или напишите автомобиль одной строкой.')
        return values
