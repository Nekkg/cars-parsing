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
