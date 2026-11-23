from django.db import models
from django.utils import timezone
from core.fields import ArrayField

class User(models.Model):
    name = models.CharField(max_length=255, null=True, blank=True)
    user_id = models.BigIntegerField(unique=True)  # Telegram ID пользователя
    monitoring_enabled = models.BooleanField(default=False)  # Включен ли мониторинг
    notifications_started_at = models.DateTimeField(null=True, blank=True)
    notification_fingerprint = models.CharField(max_length=64, blank=True, default='')

    def __str__(self):
        return f"{self.user_id} - {'Вкл' if self.monitoring_enabled else 'Выкл'}"
    
    class Meta:
        verbose_name = "Пользователь Telegram"
        verbose_name_plural = "Пользователи Telegram"


class UserFilters(models.Model):
    generation_keys = models.JSONField(default=list, blank=True)
    generation_hint = models.CharField(max_length=100,blank=True,default='')
    radius_km = models.PositiveSmallIntegerField(default=0)
    location = models.JSONField(default=dict,blank=True)
    variant = models.CharField(max_length=100, blank=True, default='')
    sort_order = models.CharField(max_length=20, default='newest')
    vehicle = models.ForeignKey('ads.CatalogNode', null=True, blank=True, on_delete=models.SET_NULL)
    search_job = models.ForeignKey('ads.SearchJob', null=True, blank=True, on_delete=models.SET_NULL)
    user_id = models.BigIntegerField(unique=True)  # Telegram ID пользователя
    platform = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Площадка
    price = models.CharField(max_length=50, blank=True, null=True)  # Цена
    brand = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Марка
    engine = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Двигатель
    mileage = models.CharField(max_length=50, blank=True, null=True)  # Пробег
    gearbox = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Коробка передач
    owners = models.CharField(max_length=50, blank=True, null=True)  # Кол-во владельцев
    condition = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Состояние
    seller = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Продавец
    city = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Город
    year = models.CharField(max_length=50, blank=True, null=True)  # Год выпуска
    body_type = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Тип кузова
    color = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Цвет кузова
    drive = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Привод
    steering = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Руль
    ad_type = ArrayField(models.CharField(max_length=50, blank=True), blank=True, default=list)  # Тип объявлений

    class Meta:
        verbose_name = "Фильтр пользователя"
        verbose_name_plural = "Фильтры пользователей"

    def __str__(self):
        return f"Фильтры пользователя {self.user_id}"

class TrackedSearch(models.Model):
    """A saved query independent of the user's current search form."""
    INTERVALS = (1, 2, 5, 10, 30)
    user_id = models.BigIntegerField(db_index=True)
    chat_id = models.BigIntegerField()
    fingerprint = models.CharField(max_length=64)
    filters = models.JSONField(default=dict)
    search_job = models.ForeignKey('ads.SearchJob', on_delete=models.PROTECT)
    interval_minutes = models.PositiveSmallIntegerField(default=5, choices=[(n, f'{n} мин') for n in INTERVALS])
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField()
    start_ad_id = models.PositiveBigIntegerField(default=0)
    next_check_at = models.DateTimeField()
    last_check_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user_id', 'fingerprint'], name='unique_user_tracked_query'),
            models.CheckConstraint(condition=models.Q(interval_minutes__in=(1, 2, 5, 10, 30)), name='valid_tracking_interval'),
        ]
        indexes = [models.Index(fields=['enabled', 'next_check_at'])]


class TrackingDelivery(models.Model):
    """Durable outbox: overlapping saved queries notify each user only once per ad."""
    user_id = models.BigIntegerField()
    chat_id = models.BigIntegerField()
    subscription = models.ForeignKey(TrackedSearch, null=True, on_delete=models.SET_NULL)
    advertisement = models.ForeignKey('ads.Advertisement', on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user_id', 'advertisement'], name='unique_tracking_user_ad')]
        indexes = [models.Index(fields=['user_id', 'sent_at']), models.Index(fields=['sent_at', 'next_attempt_at'])]
