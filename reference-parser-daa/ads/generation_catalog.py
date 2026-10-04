"""Lazy catalogue loading for any selected model; shared by the source workers."""
import logging
import threading
import time
from datetime import timedelta
from django.utils import timezone
from ads.models import CatalogNode
from ads.catalog import load_children

logger = logging.getLogger(__name__)
_guard = threading.Lock()
_locks = {}
_failed_until = {}


def ensure_generation_catalog(filters):
    vehicle = filters.get('vehicle',{})
    make, model = vehicle.get('make',{}),vehicle.get('model',{})
    if not make or not model:
        return
    node = CatalogNode.objects.filter(kind='model',parent__slug=make.get('slug'),slug=model.get('slug')).first()
    if not node or not node.data.get('url'):
        return
    if node.data.get('canonical_model_key'):
        node = CatalogNode.objects.filter(kind='model',key=node.data['canonical_model_key'],parent=node.parent).first() or node
    with _guard:
        lock = _locks.setdefault(node.pk,threading.Lock())
    with lock:
        if _failed_until.get(node.pk,0) > time.monotonic():
            return
        node.refresh_from_db()
        children = CatalogNode.objects.filter(parent=node,kind='generation')
        fresh = node.refreshed_at and node.refreshed_at > timezone.now()-timedelta(days=7)
        metadata = list(children.values_list('data',flat=True))
        enriched = bool(metadata) and all(data.get('schema_version') == 2 for data in metadata)
        if fresh and enriched:
            return
        try:
            loaded = load_children(node.pk,force=not enriched)
            if not loaded or any(child.data.get('schema_version') != 2 for child in loaded):
                _failed_until[node.pk] = time.monotonic()+300
        except Exception as error:
            _failed_until[node.pk] = time.monotonic()+300
            logger.warning('Generation catalogue unavailable model=%s type=%s',node.key,type(error).__name__)
