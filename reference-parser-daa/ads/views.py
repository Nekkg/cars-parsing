"""Local multi-source search: session isolation, CSRF protected requests, partial results."""
import json
from datetime import timedelta
from difflib import SequenceMatcher
from functools import lru_cache
from django.core.cache import cache
from django.http import JsonResponse, Http404
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from .models import Advertisement, CatalogNode, WebSearch
from .catalog import load_children
from .catalog_search import ranked_rows
from .forms import WebSearchForm, FILTER_CHOICES
from .filter_schema import FILTER_LABELS, INPUT_ALIASES
from .geography import cities, norm, ALIASES
from .search import enqueue, SORT_FIELDS
from .web_search import result_snapshot, stored_snapshot, public_ad, expire_owner_searches


def failure(message, status=400, errors=None):
    return JsonResponse({'error': message, 'errors': errors or {}}, status=status)


def payload(request):
    if len(request.body) > 8192:
        raise ValueError('Слишком много данных в запросе.')
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Не удалось прочитать запрос.') from None
    if not isinstance(data, dict): raise ValueError('Ожидались поля поиска.')
    return data


def owner_key(request, create=False):
    if not request.session.session_key and create:
        request.session.create()
    return salted_hmac('web-search-owner', request.session.session_key, algorithm='sha256').hexdigest() if request.session.session_key else None


def owned_search(request, search_id):
    return get_object_or_404(WebSearch.objects.select_related('search_job'), pk=search_id, owner_key=owner_key(request))


@require_GET
@ensure_csrf_cookie
def index(request):
    current = request.session.get('web_search_id')
    search = WebSearch.objects.filter(pk=current, owner_key=owner_key(request)).first() if current else None
    return render(request, 'ads/index.html', {'search_id':str(search.pk) if search else None,
        'filter_choices': FILTER_CHOICES,
        'filter_fields':[dict(name=field,label=FILTER_LABELS[field],options=choices,aliases=INPUT_ALIASES.get(field,{})) for field,choices in FILTER_CHOICES.items()]})


@require_GET
def catalog_options(request):
    parent_id = request.GET.get('parent')
    if parent_id and not parent_id.isdigit(): return failure('Некорректный раздел каталога.')
    parent = get_object_or_404(CatalogNode, pk=parent_id) if parent_id else None
    if parent and parent.kind == 'generation': return failure('У поколения нет дочерних вариантов.')
    query = request.GET.get('q', '')[:80]
    try:
        children = load_children(parent.pk if parent else None)
    except Exception:
        return failure('Каталог временно недоступен. Попробуйте ещё раз.', 503)
    make = parent if parent and parent.kind == 'make' else parent.parent if parent else None
    rows = ranked_rows(children, query, make_slug=make.slug if make else '')
    # Generations are always returned in full, in production order.
    limit = 100 if parent and parent.kind == 'model' else 40
    return JsonResponse({'options':[dict(id=n.pk, name=n.name, kind=n.kind, key=n.key,
        year_from=n.data.get('year_from'), year_to=n.data.get('year_to')) for n in rows[:limit]]})


@lru_cache(maxsize=256)
def city_suggestions(query):
    """Bounded cache for the immutable local city directory, shared by sessions."""
    key = norm(query)
    target = norm(ALIASES.get(query.strip().casefold(), query))
    popular = ('Москва','Санкт-Петербург','Самара','Казань','Екатеринбург','Нижний Новгород','Новосибирск','Краснодар')
    scored = []
    for c in cities():
        name = norm(c['name'])
        score = 4 if name == target else 3 if name.startswith(target) else 2 if target in name else 0
        if key and not score and len(key) >= 3:
            ratio = SequenceMatcher(None, target, name).ratio()
            score = ratio if ratio >= .75 else 0
        if key and not score: continue
        priority = popular.index(c['name']) if c['name'] in popular else len(popular)
        scored.append((-score, priority, c['name'], c['region'], c['id'], c))
    return [dict(id=c['id'], name=c['name'], region=c['region']) for *_, c in sorted(scored)[:20]]


@require_GET
def city_options(request):
    query = request.GET.get('q', '')[:100].strip().casefold()
    return JsonResponse({'options':city_suggestions(query)})


@require_POST
def parse_query(request):
    try:
        data = payload(request)
        text = data.get('text')
        if not isinstance(text, str) or not text.strip(): raise ValueError('Напишите автомобиль и условия поиска.')
        from .vehicle_query import resolve_query
        resolved = resolve_query(text)
        if not resolved: return failure('Не удалось распознать автомобиль. Уточните марку и модель или используйте фильтры.')
        model_ids = [pk for pk, name in resolved['models']]
        models = CatalogNode.objects.filter(pk__in=model_ids).select_related('parent')
        by_id = {n.pk:n for n in models}
        resolved['models'] = [dict(id=pk, name=name, make_id=by_id[pk].parent_id, make_name=by_id[pk].parent.name)
                               for pk, name in resolved['models'] if pk in by_id]
        if not resolved['models'] and resolved['model_text']:
            return failure('Модель не найдена. Уточните написание или выберите её в фильтрах.')
        return JsonResponse(resolved)
    except ValueError as error:
        return failure(str(error))
    except Exception:
        return failure('Не удалось загрузить каталог. Попробуйте ещё раз.', 503)


@require_POST
def create_search(request):
    try: data = payload(request)
    except ValueError as error: return failure(str(error))
    owner = owner_key(request, create=True)
    throttle = 'web-search-rate:'+owner
    if not cache.add(throttle, 1, 60):
        if cache.incr(throttle) > 15:
            return failure('Слишком частые поиски. Подождите минуту.', 429)
    form = WebSearchForm(data)
    if not form.is_valid(): return failure('Проверьте условия поиска.', errors=dict(form.errors))
    job = enqueue(form.filters)
    expire_owner_searches(owner)
    search = WebSearch.objects.create(owner_key=owner, search_job=job, filters=form.filters,
                                    collect_until=timezone.now()+timedelta(minutes=30))
    request.session['web_search_id'] = str(search.pk)
    return JsonResponse(result_snapshot(search), status=201)


def result_options(request):
    sort_order = request.GET.get('sort', 'newest')
    platform = request.GET.get('platform', 'all')
    if sort_order not in SORT_FIELDS or platform not in ('all','avito','autoru','drom'):
        raise ValueError('Неподдерживаемый порядок или площадка.')
    try: page = max(1, min(100000, int(request.GET.get('page', 1))))
    except (TypeError, ValueError): raise ValueError('Некорректный номер страницы.') from None
    return sort_order, platform, page


@require_GET
def stored_feed(request):
    try: options = result_options(request)
    except ValueError as error: return failure(str(error))
    return JsonResponse(stored_snapshot(*options))


@require_GET
def search_results(request, search_id):
    search = owned_search(request, search_id)
    try: options = result_options(request)
    except ValueError as error: return failure(str(error))
    return JsonResponse(result_snapshot(search, *options))


@require_POST
def refresh_search(request, search_id):
    search = owned_search(request, search_id)
    search.search_job = enqueue(search.filters)
    search.collect_until = timezone.now()+timedelta(minutes=30)
    search.save(update_fields=['search_job','collect_until'])
    return JsonResponse({'ok':True})


@require_GET
def ad_detail(request, ad_id):
    ad = get_object_or_404(Advertisement, ad_id=ad_id)
    return render(request, 'ads/detail.html', {'car':public_ad(ad), 'ad':ad})
