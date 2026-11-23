from users.models import UserFilters
from core.fields import ArrayField
from django.db import models
from asgiref.sync import sync_to_async
from ads.search import request_search
from users.models import User

async def get_user_filters_explain() -> str:
    """ Получает фильтры пользователя из БД и форматирует их для вывода """

    return (
        f"⚙️ *Описание фильтров*\n\n"
        f"• *Выбранные площадки:* _drom, autoru, avito_ \n"
        f"• *Выбранный год выпуска:* _1900-2025_ \n"
        f"• *Выбранная цена:* _0-99999999_ \n"
        f"• *Выбранные города:* _название города_ \n"
        f"• *Выбранные марки:* _название бренда_ \n"
        f"• *Выбранный диапазон пробега:* _0-99999999_ \n"
        f"• *Выбранный тип двигателя:* _дизель, бензин, газ, гибрид_ \n"
        f"• *Выбранный цвет кузова:* _цвет_ \n"
        f"• *Выбранный вид коробки передач:* _робот, механика, вариатор, автомат_ \n"
        f"• *Выбранный привод:* _передний, задний, полный_ \n"
        f"• *Выбранный руль:* _левый, правый_ \n"
        f"• *Выбранное кол-во владельцев:* _0-99_ \n"
        f"• *Выбранный тип кузова:* _тип кузова_ \n"
        f"• *Выбранное состояние:* _битый, не битый_ \n"
        f"• *Выбранный тип продавца:* _автодилер, частное лицо_ \n"
    )

async def get_user_filters(user_id: int) -> str:
    from ads.filter_values import describe
    from ads.search import snapshot
    from html import escape
    filters = await get_user_filters_from_db(user_id)
    return escape(await sync_to_async(lambda: describe(snapshot(filters)))())

async def get_user_filters_from_db(user_id: int):
    filters, _ = await UserFilters.objects.aget_or_create(user_id=user_id)
    return filters

async def update_user_filter(user_id: int, field: str, value):
    """
    Обновляет значение фильтра для пользователя.
    
    Параметр value может быть:
    - Строкой: обычное значение или строка с разделителями для массива
    - Списком: прямая установка массива значений
    - None: очистка значения
    """
    filters, _ = await UserFilters.objects.aget_or_create(user_id=user_id)
    await User.objects.filter(user_id=user_id).aupdate(monitoring_enabled=False)
    filters.search_job = None
    if field == 'brand':
        filters.vehicle = None
        filters.generation_hint = ''
        filters.generation_keys = []
        filters.variant = ''
    if field == 'city':
        filters.radius_km = 0
        filters.location = {}
    
    # Получаем объект поля по его имени
    model_field = UserFilters._meta.get_field(field)
    
    # Если значение уже является списком, используем его как есть
    if isinstance(value, list):
        setattr(filters, field, value)
    # Если это ArrayField и значение - строка с разделителями
    elif isinstance(model_field, ArrayField) and isinstance(value, str) and "," in value:
        new_values = [v.strip() for v in value.split(",")]
        setattr(filters, field, new_values)
    # Если это ArrayField, но значение - одиночная строка
    elif isinstance(model_field, ArrayField) and isinstance(value, str):
        # Преобразуем одиночное значение в список с одним элементом
        setattr(filters, field, [value])
    # Обычное текстовое поле
    else:
        setattr(filters, field, value)
    
    await filters.asave()
    if await User.objects.filter(user_id=user_id, monitoring_enabled=True).aexists():
        await sync_to_async(request_search)(filters)
    
async def get_user_filter_values(user_id: int, field: str):
    filters = await get_user_filters_from_db(user_id)
    value = getattr(filters, field)
    
    # Если значение не пустое и не список, преобразуем его в список с одним элементом
    if value and not isinstance(value, list):
        return [value]
    
    return value or []

async def clear_user_filter(user_id: int, field: str):
    filters, _ = await UserFilters.objects.aget_or_create(user_id=user_id)
    await User.objects.filter(user_id=user_id).aupdate(monitoring_enabled=False)
    filters.search_job = None
    if field == 'brand':
        filters.vehicle = None
        filters.generation_hint = ''
        filters.generation_keys = []
        filters.variant = ''
    if field == 'city':
        filters.radius_km = 0
        filters.location = {}
    
    # Получаем информацию о поле из модели
    field_type = UserFilters._meta.get_field(field)
    
    # Очищаем в зависимости от типа поля
    if isinstance(field_type, ArrayField):
        setattr(filters, field, [])  # Для массивов — пустой список
    elif isinstance(field_type, (models.CharField, models.TextField)):
        setattr(filters, field, "")  # Для строк — пустая строка
    elif isinstance(field_type, (models.IntegerField, models.BigIntegerField, models.FloatField)):
        setattr(filters, field, None)  # Для чисел — None (или 0, если нужно)
    else:
        setattr(filters, field, None)  # Для остальных типов — None
    
    await filters.asave()
    if await User.objects.filter(user_id=user_id, monitoring_enabled=True).aexists():
        await sync_to_async(request_search)(filters)

@sync_to_async
def update_user_location(user_id, location, radius_km=0):
    """Update the location constraint together without touching the vehicle or saved tracking."""
    from django.db import transaction
    from ads.geography import cities
    from ads.models import ResultView
    if not isinstance(radius_km, int) or isinstance(radius_km, bool) or not 0 <= radius_km <= 1000:
        raise ValueError('Радиус должен быть от 0 до 1000 км.')
    origin = next((city for city in cities() if city['id'] == location.get('id')), None)
    if origin is None:
        raise ValueError('Город отсутствует в справочнике. Выберите его заново.')
    with transaction.atomic():
        filters, _ = UserFilters.objects.select_for_update().get_or_create(user_id=user_id)
        filters.city = [origin['name']]
        filters.location = origin
        filters.radius_km = radius_km
        filters.search_job = None
        filters.save(update_fields=['city', 'location', 'radius_km', 'search_job'])
        User.objects.filter(user_id=user_id).update(monitoring_enabled=False)
        ResultView.objects.filter(user_id=user_id,live_until__isnull=False).update(live_until=None,collect_until=None)
