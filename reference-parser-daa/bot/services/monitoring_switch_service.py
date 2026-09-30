from users.models import User
from users.models import UserFilters
from ads.search import request_search
from asgiref.sync import sync_to_async
from django.utils import timezone

async def enable_monitoring(user_id: int):
    filters, _ = await UserFilters.objects.aget_or_create(user_id=user_id)
    job = await sync_to_async(request_search)(filters)
    user, _ = await User.objects.aget_or_create(user_id=user_id)
    if user:
        user.monitoring_enabled = True
        user.notifications_started_at = timezone.now()
        user.notification_fingerprint = job.fingerprint
        await user.asave()
    
async def disable_monitoring(user_id: int):
    user, _ = await User.objects.aget_or_create(user_id=user_id)
    if user:
        user.monitoring_enabled = False
        await user.asave()
