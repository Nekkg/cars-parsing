"""Saved-query lifecycle, shared periodic jobs and a deduplicated notification outbox."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

from django.db import transaction
from django.db.models import Count, Q, Max
from django.utils import timezone
from ads.models import SearchJob, Delivery, Advertisement
from ads.search import results, matches
from ads.search_status import selected_sources
from users.models import TrackedSearch, TrackingDelivery, User

INTERVALS = TrackedSearch.INTERVALS


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def view_query(view, source=None):
    data = deepcopy(view.filters)
    source = view.platform if source is None else source
    if source not in ('all', 'avito', 'autoru', 'drom'):
        raise ValueError('Неизвестная площадка.')
    if source != 'all':
        data['platform'] = [source]
    return data


def validate_query(data):
    if not data.get('vehicle') and not data.get('brand') and not data.get('variant'):
        raise ValueError('Сначала выберите автомобиль и выполните поиск.')


def validate_interval(minutes):
    if minutes not in INTERVALS:
        raise ValueError('Выберите частоту: 1, 2, 5, 10 или 30 минут.')


def follow(user_id, chat_id, data, minutes):
    validate_query(data)
    validate_interval(minutes)
    now = timezone.now()
    key = fingerprint(data)
    # Separate cycles protect a user's interactive search from periodic restarts.
    # Normalized upstream pages and the advertisement database remain shared.
    job_key = hashlib.sha256(('tracking:' + key).encode()).hexdigest()
    with transaction.atomic():
        job, _ = SearchJob.objects.get_or_create(fingerprint=job_key, defaults={'kind': 'tracking', 'filters': deepcopy(data)})
        row, created = TrackedSearch.objects.get_or_create(user_id=user_id, fingerprint=key, defaults={
            'chat_id': chat_id, 'filters': deepcopy(data), 'search_job': job,
            'interval_minutes': minutes, 'started_at': now,
            'start_ad_id': Advertisement.objects.aggregate(last=Max('pk'))['last'] or 0,
            'next_check_at': now + timedelta(minutes=minutes)})
        row = TrackedSearch.objects.select_for_update().get(pk=row.pk)
        if not row.enabled:
            row.started_at = now
            row.start_ad_id = Advertisement.objects.aggregate(last=Max('pk'))['last'] or 0
            row.last_check_at = None
        if created or not row.enabled or row.interval_minutes != minutes:
            row.next_check_at = now + timedelta(minutes=minutes)
        row.enabled = True
        row.interval_minutes = minutes
        row.chat_id = chat_id
        row.save()
        # Legacy single-query delivery cannot run in parallel for this user.
        User.objects.get_or_create(user_id=user_id)
        User.objects.filter(user_id=user_id).update(monitoring_enabled=False)
    return row


def owned(pk, user_id, chat_id):
    return TrackedSearch.objects.filter(pk=pk, user_id=user_id, chat_id=chat_id).select_related('search_job').first()


def change_interval(pk, user_id, chat_id, minutes):
    validate_interval(minutes)
    with transaction.atomic():
        row = TrackedSearch.objects.select_for_update().filter(pk=pk, user_id=user_id, chat_id=chat_id).first()
        if not row:
            return None
        row.interval_minutes = minutes
        row.next_check_at = timezone.now() + timedelta(minutes=minutes)
        row.save(update_fields=['interval_minutes', 'next_check_at'])
        return row


def set_enabled(pk, user_id, chat_id, enabled):
    with transaction.atomic():
        row = TrackedSearch.objects.select_for_update().filter(pk=pk, user_id=user_id, chat_id=chat_id).first()
        if not row:
            return None
        if enabled and not row.enabled:
            row.started_at = timezone.now()
            row.start_ad_id = Advertisement.objects.aggregate(last=Max('pk'))['last'] or 0
            row.next_check_at = row.started_at + timedelta(minutes=row.interval_minutes)
            row.last_check_at = None
        row.enabled = enabled
        row.save(update_fields=['enabled', 'started_at', 'start_ad_id', 'next_check_at', 'last_check_at'])
        if not enabled:
            TrackingDelivery.objects.filter(subscription=row, sent_at__isnull=True).delete()
        return row


def remove(pk, user_id, chat_id):
    with transaction.atomic():
        row = TrackedSearch.objects.select_for_update().filter(pk=pk, user_id=user_id, chat_id=chat_id).first()
        if not row:
            return False
        TrackingDelivery.objects.filter(subscription=row, sent_at__isnull=True).delete()
        row.delete()  # Sent rows retain the user's deduplication history.
        return True


def stop_user(user_id):
    with transaction.atomic():
        TrackedSearch.objects.filter(user_id=user_id).update(enabled=False)
        TrackingDelivery.objects.filter(user_id=user_id, sent_at__isnull=True).delete()
        User.objects.get_or_create(user_id=user_id)
        User.objects.filter(user_id=user_id).update(monitoring_enabled=False)


def schedule_due_checks():
    """No overlapping crawl; users with the same query share one periodic job."""
    now = timezone.now()
    due_jobs = list(TrackedSearch.objects.filter(enabled=True, next_check_at__lte=now)
                    .values_list('search_job_id', flat=True).distinct()[:100])
    started = []
    for pk in due_jobs:
        with transaction.atomic():
            job = SearchJob.objects.select_for_update(skip_locked=True).filter(pk=pk).first()
            if not job:
                continue
            due = list(TrackedSearch.objects.select_for_update(skip_locked=True)
                       .filter(search_job=job, enabled=True, next_check_at__lte=now))
            if not due:
                continue
            active = TrackedSearch.objects.filter(search_job=job, enabled=True)
            already_started = active.filter(last_check_at__isnull=False).exists()
            token = job.requested_at.isoformat()
            busy = any(job.source_state.get(s, {}).get('requested_at') != token
                       and job.source_state.get(s, {}).get('error_code') != 'blocked'
                       for s in selected_sources(job.filters))
            if already_started and busy:
                for row in due:
                    row.next_check_at = now + timedelta(seconds=10)
                TrackedSearch.objects.bulk_update(due, ['next_check_at'])
                continue
            fastest = min(active.values_list('interval_minutes', flat=True))
            if not already_started or job.requested_at <= now - timedelta(minutes=fastest):
                job.requested_at = now
                job.save(update_fields=['requested_at'])
                started.append(job.pk)
            for row in due:
                row.last_check_at = now
                row.next_check_at = now + timedelta(minutes=row.interval_minutes)
            TrackedSearch.objects.bulk_update(due, ['last_check_at', 'next_check_at'])
    return started


def queue_new_ads():
    """Evaluate identical saved queries once; never send an existing database backlog."""
    groups = {}
    for row in TrackedSearch.objects.filter(enabled=True, last_check_at__isnull=False):
        groups.setdefault(row.fingerprint, []).append(row)
    pending = []
    new_pairs = set()
    now = timezone.now()
    for watches in groups.values():
        cutoff = min(row.started_at for row in watches)
        eligible = Q()
        # Exclude handled ads in SQL before loading/normalizing matching records.
        # Old rows may still become eligible when delayed generation details arrive.
        for row in watches:
            known = TrackingDelivery.objects.filter(user_id=row.user_id).values_list('advertisement_id', flat=True)
            legacy = Delivery.objects.filter(user_id=row.user_id).values_list('advertisement_id', flat=True)
            eligible |= (Q(created_at__gte=row.started_at, pk__gt=row.start_ad_id)
                         & (Q(published_at__isnull=True) | Q(published_at__gte=row.started_at))
                         & ~Q(pk__in=known) & ~Q(pk__in=legacy))
        ads = results(watches[0].filters, defer_media=True, created_since=cutoff, candidate_filter=eligible)
        if not ads:
            continue
        user_ids = {row.user_id for row in watches}
        ad_ids = [ad.pk for ad in ads]
        existing = set(TrackingDelivery.objects.filter(user_id__in=user_ids, advertisement_id__in=ad_ids)
                       .values_list('user_id', 'advertisement_id'))
        existing.update(Delivery.objects.filter(user_id__in=user_ids, advertisement_id__in=ad_ids)
                        .values_list('user_id', 'advertisement_id'))
        for row in watches:
            for ad in ads:
                key = (row.user_id, ad.pk)
                if key in existing or key in new_pairs or ad.created_at < row.started_at or ad.pk <= row.start_ad_id:
                    continue
                if ad.published_at and ad.published_at < row.started_at:
                    continue
                new_pairs.add(key)
                pending.append(TrackingDelivery(user_id=row.user_id, chat_id=row.chat_id,
                    subscription=row, advertisement=ad, created_at=now, next_attempt_at=now))
    TrackingDelivery.objects.bulk_create(pending, ignore_conflicts=True, batch_size=500)
    return len(pending)


def claim_delivery():
    now = timezone.now()
    cutoff = now - timedelta(minutes=1)
    occupied = {}
    rows = TrackingDelivery.objects.filter(Q(sent_at__gte=cutoff) | Q(sent_at__isnull=True, lease_until__gt=now))
    for entry in rows.values('user_id').annotate(total=Count('id')):
        occupied[entry['user_id']] = entry['total']
    for entry in Delivery.objects.filter(sent_at__gte=cutoff).values('user_id').annotate(total=Count('id')):
        occupied[entry['user_id']] = occupied.get(entry['user_id'], 0) + entry['total']
    blocked_users = {user for user, count in occupied.items() if count >= 3}
    with transaction.atomic():
        while True:
            row = (TrackingDelivery.objects.select_for_update(of=('self',), skip_locked=True)
                   .filter(sent_at__isnull=True, next_attempt_at__lte=now, subscription__enabled=True)
                   .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
                   .exclude(user_id__in=blocked_users).select_related('subscription', 'advertisement')
                   .order_by('created_at', 'id').first())
            if not row:
                return None
            user = User.objects.select_for_update(skip_locked=True).filter(user_id=row.user_id).first()
            if not user:
                blocked_users.add(row.user_id)
                continue
            # Recheck after acquiring the user lock, including in-flight sends.
            recent = TrackingDelivery.objects.filter(user_id=row.user_id).filter(
                Q(sent_at__gte=cutoff) | Q(sent_at__isnull=True, lease_until__gt=now)).count()
            recent += Delivery.objects.filter(user_id=row.user_id, sent_at__gte=cutoff).count()
            if recent >= 3:
                blocked_users.add(row.user_id)
                continue
            if row.advertisement.created_at < row.subscription.started_at or not matches(row.advertisement.__dict__, row.subscription.filters):
                row.delete()
                continue
            row.lease_until = now + timedelta(minutes=3)
            row.attempts += 1
            row.save(update_fields=['lease_until', 'attempts'])
            return row


def still_allowed(pk, lease):
    return TrackingDelivery.objects.filter(pk=pk, lease_until=lease, sent_at__isnull=True, subscription__enabled=True).exists()


def sent(pk, lease):
    TrackingDelivery.objects.filter(pk=pk, lease_until=lease, sent_at__isnull=True).update(sent_at=timezone.now(), lease_until=None)


def retry(pk, lease, seconds):
    TrackingDelivery.objects.filter(pk=pk, lease_until=lease, sent_at__isnull=True).update(
        lease_until=None, next_attempt_at=timezone.now() + timedelta(seconds=max(1, seconds)))
