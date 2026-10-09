from django.db import models
from asgiref.sync import sync_to_async

class Parser(models.Model):
    name = models.CharField(max_length=255, unique=True)  # Уникальное имя
    url = models.URLField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Парсер"
        verbose_name_plural = "Парсеры"
        ordering = ["name"]  # Упорядочивание по алфавиту

    def __str__(self):
        status = "🟢" if self.is_active else "🔴"
        return f"{status} {self.name}"
    
    @classmethod
    async def get_active_parsers(cls):
        return await sync_to_async(list)(cls.objects.filter(is_active=True))


class SourceFetch(models.Model):
    """Shared normalized results, never browser profiles or raw challenge HTML."""
    key = models.CharField(max_length=64, primary_key=True)
    source = models.CharField(max_length=20)
    payload = models.JSONField(default=dict)
    expires_at = models.DateTimeField(db_index=True)
    lease_until = models.DateTimeField(null=True, blank=True)


class MediaRequest(models.Model):
    advertisement = models.OneToOneField('ads.Advertisement',on_delete=models.CASCADE,primary_key=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    next_attempt_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True,blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=20,blank=True,default='')

class VehicleAudit(models.Model):
    """One evidence-bearing verification per catalogue model and platform."""
    model = models.ForeignKey('ads.CatalogNode',on_delete=models.CASCADE)
    source = models.CharField(max_length=20)
    status = models.CharField(max_length=24,default='pending',db_index=True)
    checked_at = models.DateTimeField(null=True,blank=True)
    lease_until = models.DateTimeField(null=True,blank=True)
    pause_until = models.DateTimeField(null=True,blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    evidence = models.JSONField(default=dict,blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['model','source'],name='unique_vehicle_source_audit')]