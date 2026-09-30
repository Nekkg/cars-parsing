from datetime import timedelta
from django.utils import timezone
from users.models import User, UserFilters
from .models import Delivery
from .search import results, snapshot


def notification_allowed(user_id, ad):
    user=User.objects.filter(user_id=user_id,monitoring_enabled=True).first()
    if not user or not user.notifications_started_at or ad.created_at < user.notifications_started_at:
        return False
    filters=UserFilters.objects.filter(user_id=user_id).select_related('search_job').first()
    if not filters or not filters.search_job_id or filters.search_job.fingerprint != user.notification_fingerprint:
        return False
    return Delivery.objects.filter(user_id=user_id,sent_at__gte=timezone.now()-timedelta(minutes=1)).count() < 3


def pending_deliveries():
    pairs=[]
    for user in User.objects.filter(monitoring_enabled=True,notifications_started_at__isnull=False):
        filters=UserFilters.objects.filter(user_id=user.user_id).select_related('vehicle','search_job').first()
        if not filters or not filters.search_job_id or filters.search_job.fingerprint != user.notification_fingerprint:
            continue
        remaining=3-Delivery.objects.filter(user_id=user.user_id,sent_at__gte=timezone.now()-timedelta(minutes=1)).count()
        if remaining <= 0: continue
        sent=set(Delivery.objects.filter(user_id=user.user_id).values_list('advertisement_id','price'))
        fresh=[ad for ad in results(snapshot(filters)) if ad.created_at >= user.notifications_started_at and (ad.pk,ad.price) not in sent]
        pairs.extend((user.user_id,ad) for ad in fresh[:remaining])
    return pairs
