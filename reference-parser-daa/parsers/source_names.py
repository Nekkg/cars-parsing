"""Exact source names and routes learned from official public catalogues."""
import json
from time import monotonic
from functools import lru_cache
from pathlib import Path
from ads.search import canonical

REGISTRY = Path(__file__).resolve().parent.parent / 'ads' / 'data' / 'source_names.json'
# Different spellings of the same model, not a fuzzy matching rule.
MODEL_ALIASES = {
    'land_rover/range_rover_evoque': ('Evoque',),
    'audi/a4_allroad_quattro': ('A4 allroad','a4_allroad'),
    'audi/a6_allroad_quattro': ('A6 allroad','allroad'),
    'audi/e-tron_sportback_s': ('e-tron S Sportback','e_tron_s_sportback'),
    'audi/e5_sportback': ('E5','e5'),
    'audi/q6_e-tron_sportback': ('Q6 Sportback e-tron','q6_sportback_e_tron'),
    'audi/sq6_e-tron_sportback': ('SQ6 Sportback e-tron','sq6_sportback_e_tron'),
    'bmw/2-series_active_tourer': ('2activetourer','2 серии Active Tourer'),
    'bmw/2-series_gran_tourer': ('2grandtourer','2 серии Gran Tourer'),
    'bmw/neue_klasse': ('New Class','new_class'),
    'honda/stepwgn': ('stepwagon', 'step wagon'),
    'mazda/mazda2': ('2',), 'mazda/mazda3': ('3',), 'mazda/mazda5': ('5',),
    'mazda/mazda6': ('6',), 'mazda/mazda8': ('8',), 'mazda/mazda6e': ('6e',),
    'mazda/mazda3_mps': ('3 MPS',), 'mazda/mazda6_mps': ('6 MPS',),
    'mercedes-benz/glc_coupe': ('GLC-Class Coupe', 'glc_klasse_coupe'),
    'mercedes-benz/gle_coupe': ('GLE-Class Coupe', 'gle_klasse_coupe'),
    'mercedes-benz/glb': ('GLB-Class', 'glb_klasse'),
    'mercedes-benz/amg_gt': ('AMG GT', 'amg gt'),
    'lada/2121_4x4_niva': ('2121',), 'lada/2131_4x4_niva': ('2131',),
    'changan/qiyuan_a05': ('A05',), 'changan/qiyuan_a06': ('A06',),
    'changan/qiyuan_a07': ('A07',), 'changan/qiyuan_e07': ('E07',),
    'changan/qiyuan_q05': ('Q05',), 'changan/qiyuan_q06': ('Q06',),
    'changan/qiyuan_q07': ('Q07',),
}
MAKE_ALIASES = (
    ('cheryexeed','exeed'), ('solaris-agr','solaris'), ('volga-mark','volga'),
    ('ssang_yong','ssangyong'), ('kg_mobility','kg mobility'),
)

@lru_cache(maxsize=2)
def _load(stamp):
    return json.loads(REGISTRY.read_text(encoding='utf-8')) if stamp else {}

_next_check=0.0
_registry={}

def registry():
    global _next_check,_registry
    if monotonic()>=_next_check:
        _registry=_load(REGISTRY.stat().st_mtime_ns if REGISTRY.exists() else 0)
        _next_check=monotonic()+5
    return _registry

def extra_aliases(node):
    values=set(MODEL_ALIASES.get(node.key,())) if node.kind=='model' else set()
    if node.kind=='make':
        for group in MAKE_ALIASES:
            if canonical(node.slug) in {canonical(value) for value in group}:values.update(group)
    entry=registry().get(node.kind+'s',{}).get(node.key,{})
    for source in entry.values():
        values.update(source.get('aliases',()))
    return values

def binding(source,make_key,model_key=None):
    data=registry()
    return data.get('models' if model_key else 'makes',{}).get(model_key or make_key,{}).get(source)


def source_model_slug(make_slug,model_slug):
    if make_slug=='infiniti':
        from .model_names import infiniti_badge,INFINITI_FAMILIES
        badge=infiniti_badge(model_slug)
        if badge:return INFINITI_FAMILIES[badge[0]]
    if make_slug=='bmw':
        import re
        found=re.fullmatch(r'([356])[-_]series_gran_turismo',model_slug)
        if found:return found[1]+'er'
    if make_slug=='mazda':
        import re
        return re.sub(r'^mazda(?=\d)', '', model_slug)
    overrides={'audi/a4_allroad_quattro':'a4_allroad','audi/a6_allroad_quattro':'allroad',
        'audi/e-tron_sportback_s':'e_tron_s_sportback','audi/e5_sportback':'e5',
        'audi/q6_e-tron_sportback':'q6_sportback_e_tron','audi/sq6_e-tron_sportback':'sq6_sportback_e_tron',
        'bmw/2-series_active_tourer':'2activetourer','bmw/2-series_gran_tourer':'2grandtourer','bmw/neue_klasse':'new_class','honda/stepwgn':'stepwagon','land_rover/range_rover_evoque':'evoque',
        'mercedes-benz/glc_coupe':'glc_klasse_coupe','mercedes-benz/gle_coupe':'gle_klasse_coupe',
        'mercedes-benz/glb':'glb_klasse','lada/2121_4x4_niva':'2121','lada/2131_4x4_niva':'2131'}
    return overrides.get(make_slug+'/'+model_slug,model_slug.replace('-','_'))
_make_data=None
_make_names=()

def make_title_names():
    global _make_data,_make_names
    data=registry()
    if data is not _make_data:
        values=set(data.get('make_names',()))
        for entry in data.get('makes',{}).values():
            for source in entry.values():values.update(source.get('aliases',()))
        _make_names=tuple(sorted(values,key=len,reverse=True))
        _make_data=data
    return _make_names