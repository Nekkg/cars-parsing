"""Validate optional source timestamps; never turn a refresh into a publication."""
from datetime import datetime, timedelta, timezone as dt_timezone
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def source_datetime(value):
    try:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, (int, float)) or isinstance(value, str) and value.isdigit():
            seconds = float(value)
            if seconds > 10**12:
                seconds /= 1000
            result = datetime.fromtimestamp(seconds, dt_timezone.utc)
        elif isinstance(value, str):
            result = parse_datetime(value)
        else:
            return None
        if result is None or timezone.is_naive(result):
            return None
        if result.year < 2000 or result > timezone.now() + timedelta(days=1):
            return None
        return result
    except (ValueError, OverflowError, OSError, TypeError):
        return None
