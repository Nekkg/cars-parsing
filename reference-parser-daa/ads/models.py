from django.db import models
import json
import uuid
from datetime import datetime
from django.utils import timezone


class CatalogNode(models.Model):
    kind = models.CharField(max_length=12, choices=[('make', 'Марка'), ('model', 'Модель'), ('generation', 'Поколение')])
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='children')
    key = models.CharField(max_length=300, unique=True)
    name = models.CharField(max_length=250)
    slug = models.CharField(max_length=150, blank=True)
    data = models.JSONField(default=dict, blank=True)
    refreshed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class SearchJob(models.Model):
    kind = models.CharField(max_length=12, default='search', choices=[('search', 'Поиск'), ('tracking', 'Отслеживание')])
    fingerprint = models.CharField(max_length=64, unique=True)
    filters = models.JSONField(default=dict)
    requested_at = models.DateTimeField(default=timezone.now)
    source_state = models.JSONField(default=dict, blank=True)


class Delivery(models.Model):
    user_id = models.BigIntegerField()
    advertisement = models.ForeignKey('Advertisement', on_delete=models.CASCADE)
    price = models.IntegerField()
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user_id', 'advertisement', 'price'], name='unique_user_ad_price')]

class Advertisement(models.Model):
    PLATFORM_CHOICES = [
        ('avito', 'Avito'),
        ('drom', 'Drom'),
        ('autoru', 'Auto.ru'),
    ]
    
    # Identification
    ad_id = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    link = models.URLField(max_length=500)
    
    # Basic info
    name = models.CharField(max_length=500)
    brand = models.CharField(max_length=200)
    model = models.CharField(max_length=200)
    brand_key = models.CharField(max_length=200, blank=True, default='', db_index=True)
    model_key = models.CharField(max_length=200, blank=True, default='', db_index=True)
    generation = models.CharField(max_length=250, blank=True, default='')
    generation_codes = models.JSONField(default=list, blank=True)
    generation_key = models.CharField(max_length=300, blank=True, default='', db_index=True)
    generation_evidence = models.JSONField(default=dict, blank=True)
    last_seen = models.DateTimeField(default=timezone.now)
    year = models.CharField(max_length=20, null=True, blank=True)
    price = models.IntegerField(default=0)
    city = models.CharField(max_length=200)
    image = models.URLField(max_length=500, null=True, blank=True)
    images = models.JSONField(default=list, blank=True)
    
    # Car details
    mileage = models.IntegerField(null=True, blank=True)
    engine = models.CharField(max_length=200, null=True, blank=True)
    color = models.CharField(max_length=100, null=True, blank=True)
    gearbox = models.CharField(max_length=100, null=True, blank=True)
    drive = models.CharField(max_length=100, null=True, blank=True)
    steering = models.CharField(max_length=100, null=True, blank=True)
    owners = models.CharField(max_length=50, null=True, blank=True)
    body_type = models.CharField(max_length=100, null=True, blank=True)
    condition = models.CharField(max_length=100, null=True, blank=True)
    
    # Type and seller
    ad_type = models.CharField(max_length=100, null=True, blank=True)
    seller = models.CharField(max_length=200, null=True, blank=True)
    
    # Timestamps - changed from auto_now_add to manually set in save method
    created_at = models.DateTimeField(db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        verbose_name = "Объявление"
        verbose_name_plural = "Объявления"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=['brand']),
            models.Index(fields=['city']),
            models.Index(fields=['platform']),
            models.Index(fields=['price']),
            models.Index(fields=['published_at']),
        ]
    
    def save(self, *args, **kwargs):
        from .search import canonical
        self.brand_key, self.model_key = canonical(self.brand), canonical(self.model)
        if kwargs.get('update_fields'):
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'brand_key', 'model_key'}
        # Set created_at with timezone-aware datetime if it's a new object
        if not self.pk:
            self.created_at = timezone.now()
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.platform} - {self.brand} {self.model} ({self.price} ₽)"
    
    @classmethod
    def from_redis_data(cls, ad_id, ad_data):
        """
        Create or update an Advertisement instance from Redis data
        """
        try:
            ad_dict = json.loads(ad_data) if isinstance(ad_data, str) else ad_data
            
            # Check if ad already exists
            try:
                ad = cls.objects.get(ad_id=ad_id)
                # Update existing ad
                ad.platform = ad_dict.get('platform', '')
                ad.link = ad_dict.get('link', '')
                ad.name = ad_dict.get('name', '')
                ad.brand = ad_dict.get('brand', '')
                ad.model = ad_dict.get('model', '')
                ad.year = ad_dict.get('year', '')
                ad.price = int(ad_dict.get('price', 0)) if ad_dict.get('price', 0) != 'unknown' else 0
                ad.city = ad_dict.get('city', '')
                ad.image = ad_dict.get('image', '')
                ad.mileage = int(ad_dict.get('mileage', 0)) if ad_dict.get('mileage', 0) != 'unknown' else None
                ad.engine = ad_dict.get('engine', '')
                ad.color = ad_dict.get('color', '')
                ad.gearbox = ad_dict.get('gearbox', '')
                ad.drive = ad_dict.get('drive', '')
                ad.steering = ad_dict.get('steering', '')
                ad.owners = ad_dict.get('owners', '')
                ad.body_type = ad_dict.get('body_type', '')
                ad.condition = ad_dict.get('condition', '')
                ad.ad_type = ad_dict.get('ad_type', '')
                ad.seller = ad_dict.get('seller', '')
                ad.published_at = timezone.now()
                ad.save()
                return ad
            except cls.DoesNotExist:
                # Create new ad with timezone-aware datetime
                ad = cls(
                    ad_id=ad_id,
                    platform=ad_dict.get('platform', ''),
                    link=ad_dict.get('link', ''),
                    name=ad_dict.get('name', ''),
                    brand=ad_dict.get('brand', ''),
                    model=ad_dict.get('model', ''),
                    year=ad_dict.get('year', ''),
                    price=int(ad_dict.get('price', 0)) if ad_dict.get('price', 0) != 'unknown' else 0,
                    city=ad_dict.get('city', ''),
                    image=ad_dict.get('image', ''),
                    mileage=int(ad_dict.get('mileage', 0)) if ad_dict.get('mileage', 0) != 'unknown' else None,
                    engine=ad_dict.get('engine', ''),
                    color=ad_dict.get('color', ''),
                    gearbox=ad_dict.get('gearbox', ''),
                    drive=ad_dict.get('drive', ''),
                    steering=ad_dict.get('steering', ''),
                    owners=ad_dict.get('owners', ''),
                    body_type=ad_dict.get('body_type', ''),
                    condition=ad_dict.get('condition', ''),
                    ad_type=ad_dict.get('ad_type', ''),
                    seller=ad_dict.get('seller', ''),
                    created_at=timezone.now(),
                    published_at=timezone.now(),
                )
                ad.save()
                return ad
        except Exception as e:
            print(f"Error creating/updating advertisement from Redis data: {e}")
            return None


class ResultView(models.Model):
    """A persistent, owner-bound result browser independent of Telegram FSM state."""
    id = models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    user_id = models.BigIntegerField(db_index=True)
    chat_id = models.BigIntegerField()
    message_id = models.BigIntegerField(null=True,blank=True)
    list_message_id = models.BigIntegerField(null=True,blank=True)
    filters = models.JSONField(default=dict)
    result_ids = models.JSONField(default=list)
    search_job = models.ForeignKey('SearchJob',null=True,blank=True,on_delete=models.SET_NULL)
    collect_until = models.DateTimeField(null=True,blank=True,db_index=True)
    live_until = models.DateTimeField(null=True,blank=True,db_index=True)
    live_state = models.JSONField(default=dict,blank=True)
    platform = models.CharField(max_length=20,default='all')
    sort_order = models.CharField(max_length=20,default='newest')
    position = models.PositiveIntegerField(default=0)
    photo_index = models.PositiveSmallIntegerField(default=0)
    last_photo = models.CharField(max_length=700,blank=True,default='')
    list_page = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now,db_index=True)

class WebSearch(models.Model):
    """Session-owned web view; collection joins the same queue as Telegram."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_key = models.CharField(max_length=64, db_index=True)
    search_job = models.ForeignKey(SearchJob, on_delete=models.CASCADE)
    filters = models.JSONField(default=dict)
    collect_until = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
