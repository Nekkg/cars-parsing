"""Catalogue-driven generation normalization for every make/model, with provenance."""
import re
from collections import defaultdict
from functools import lru_cache
from ads.search import canonical
from ads.generation_labels import ordinal, facelift, production_range

VERSION = 5
MAKE_ALIASES = (
    ('lada','vaz','ваз','лада','Lada (ВАЗ)','ВАЗ (LADA)'), ('mercedes-benz','mercedes','мерседесбенц'),
    ('gaz','газ'), ('uaz','уаз'), ('izh','иж'),
    ('moskvich','moskvitch','moscvich','москвич'),
    ('li','lixiang','li auto'), ('lynk & co','lynkco','lynkandco'),
)


# Official source families differ from Drom's body/production phases for Granta.
# Auto.ru catalogue: /catalog/cars/vaz/granta/7684102/ (I, 2011-2018),
# Avito detail labels: I (2011-2018), I рестайлинг (2018-н.в.).
# An explicit source generation is required; years alone never assign a family.
SOURCE_GENERATION_PERIODS = {
    ('lada/granta', 'avito', 1, 0): (2011, 2018),
    ('lada/granta', 'avito', 1, 1): (2018, None),
    ('lada/granta', 'autoru', 1, 0): (2011, 2018),
    ('lada/granta', 'autoru', 1, 1): (2018, None),
    # Observed Avito detail: III рестайлинг (2009—2016). This source family
    # spans more than one regional production phase in the Drom catalogue.
    ('toyota/rav4', 'avito', 3, 1): (2009, 2016),
}
SOURCE_CATALOGUE_PERIODS = {
    ('toyota/rav4', 'avito', 3, 1): (2008, 2016),
}


def title_generation(title):
    # The suffix can include body codes, market names and numbered facelifts.
    match = re.search(r'\b([IVX]+)(?=\s*(?:\(|[,]|Рестайл|Restyl|Facelift|China\s+Market|US\s+Market))(.*?)(?=,\s*(?:19|20)\d{2}\b|$)', title)
    if match and ordinal(match[1]):
        return (match[1]+match[2]).strip()
    match = re.search(r'\b([IVX]+)(?=\s*,\s*(?:19|20)\d{2}\b|\s*$)',title)
    return match[1] if match and ordinal(match[1]) else ''


def aliases(node):
    from .source_names import extra_aliases
    values = {node.name, node.slug, *node.data.get('aliases', []), *extra_aliases(node)}
    keys = {canonical(value) for value in values}
    if node.kind == 'make':
        for group in MAKE_ALIASES:
            if keys.intersection(canonical(value) for value in group):
                values.update(group)
    if node.kind == 'model' and node.parent and canonical(node.parent.slug) == 'lada':
        # Repair stored Avito listings parsed before the full make-name fix.
        values.update(prefix + value for value in list(values) for prefix in ('(LADA) ', '(ВАЗ) '))
    if node.kind == 'model' and node.parent and canonical(node.parent.slug) == 'bmw':
        match = re.fullmatch(r'([1-8])[-_ ]series',node.slug,re.I)
        if match:
            digit = match[1]
            values.update((digit,digit+'er',digit+' серия',digit+' серии'))
    if node.kind == 'model' and node.parent and canonical(node.parent.slug) in ('mercedesbenz','mercedes'):
        for value in list(values):
            match = re.fullmatch(r'([A-Za-z]+)[-_ ](?:class|klasse|класс)',value,re.I)
            if match:
                values.update(match[1]+suffix for suffix in ('-Class','_klasse','-Класс'))
    return values - {''}


@lru_cache(maxsize=8192)
def prefix_pattern(alias):
    parts = re.findall(r'[^\W_]+|[+&]',str(alias).strip())
    pattern = r'(?:[^\w+&]|_)*'.join(re.escape(part) for part in parts)
    return re.compile(r'^\s*'+pattern+r'(?=\W|$)',re.I) if pattern else None


def prefix(text, alias):
    pattern = prefix_pattern(alias)
    return pattern.match(text) if pattern else None


@lru_cache(maxsize=8192)
def code_pattern(code):
    parts = re.findall(r'[A-Za-zА-Яа-я]+|\d+',str(code))
    pattern = r'[\s_-]*'.join(re.escape(part) for part in parts)
    return re.compile(r'(?<!\w)'+pattern+r'(?!\w)',re.I) if pattern else None


def contains_code(label, code):
    pattern = code_pattern(code)
    return bool(pattern and pattern.search(label))

class GenerationIndex:
    def __init__(self, generations=(), models=(), makes=()):
        self.nodes = list(generations)
        self.by_model = defaultdict(list)
        self.models = {model.key:model for model in models}
        self.makes = {make.key:make for make in makes}
        self.unscoped = []
        self.family_codes = defaultdict(set)
        self.source_ordinals = defaultdict(set)
        for node in self.nodes:
            model = node.parent if node.parent_id else None
            if model and model.parent:
                self.models[model.key] = model
                self.makes[model.parent.key] = model.parent
                self.by_model[model.key].append(node)
            else:
                self.unscoped.append(node)
        for model_key, nodes in self.by_model.items():
            for node in nodes:
                number = node.data.get('generation_number') or ordinal(node.data.get('description',''))
                if number:
                    self.family_codes[(model_key, number)].update(node.data.get('codes',[]))
        self.make_aliases = defaultdict(list)
        self.model_aliases = defaultdict(list)
        self.model_prefixes = defaultdict(list)
        self.make_prefixes = []
        for make in self.makes.values():
            for alias in aliases(make):
                self.make_aliases[canonical(alias)].append(make)
                self.make_prefixes.append((alias,make))
        for model in self.models.values():
            for alias in aliases(model):
                self.model_aliases[(model.parent.key,canonical(alias))].append(model)
                self.model_prefixes[model.parent.key].append((alias,model))
        self.make_prefixes.sort(key=lambda value:len(value[0]),reverse=True)
        for values in self.model_prefixes.values():
            values.sort(key=lambda value:len(value[0]),reverse=True)

    @classmethod
    def from_db(cls):
        from ads.models import CatalogNode, Advertisement
        index = cls(CatalogNode.objects.filter(kind='generation').select_related('parent__parent'),
                    CatalogNode.objects.filter(kind='model').select_related('parent'),
                    CatalogNode.objects.filter(kind='make'))
        nodes = {node.key:node for node in index.nodes}
        verified = Advertisement.objects.filter(generation_evidence__status='resolved',
            generation_evidence__method__in=('body_code','catalog_url','label_range')).order_by().values_list(
                'generation_key','platform','generation_evidence__label').distinct()
        for key, source, label in verified:
            node = nodes.get(key)
            if node:
                index.learn_ordinal(node,source,label)
        return index

    def learn_ordinal(self, node, source, label):
        order = ordinal(label)
        family = node.data.get('generation_number') or ordinal(node.data.get('description',''))
        if node.parent_id and order and family:
            self.source_ordinals[(node.parent.key,source,order)].add(family)

    def learn_from_ads(self, rows):
        nodes = {node.key:node for node in self.nodes}
        for row in rows:
            ad = dict(row.__dict__)
            self.normalize(ad)
            evidence = ad['generation_evidence']
            if evidence['status'] == 'resolved' and evidence['method'] in ('body_code','catalog_url','label_range'):
                self.learn_ordinal(nodes[ad['generation_key']],ad.get('platform',''),evidence['label'])

    def codes_for(self, node, model, phase):
        codes = set(node.data.get('codes',[])) | set(node.data.get('frame_codes',[]))
        # Some sources keep the original body name after a facelift (XV50/XV55).
        if model and phase:
            number = node.data.get('generation_number') or ordinal(node.data.get('description',''))
            codes.update(self.family_codes.get((model.key,number),()))
        return codes

    def identify_make(self, ad):
        choices = self.make_aliases.get(canonical(ad.get('brand','')), [])
        unique = {node.key:node for node in choices}
        if len(unique) == 1:
            return next(iter(unique.values()))
        for alias,make in self.make_prefixes:
            if prefix(ad.get('name',''),alias):
                return make
        return None

    def canonical_model(self, node):
        return self.models.get(node.data.get('canonical_model_key'),node)

    def identify_model(self, ad):
        make = self.identify_make(ad)
        if not make:
            return None
        from .model_names import explicit_family_model
        family_model=self.models.get(explicit_family_model(ad))
        if family_model and family_model.parent.key==make.key:return self.canonical_model(family_model)
        if canonical(make.slug) == 'lexus':
            from .model_names import lexus_model_slug, lexus_badge
            inferred = lexus_model_slug(ad)
            candidate = self.models.get(make.key+'/'+inferred) if inferred else None
            raw = canonical(ad.get('model', ''))
            badge = lexus_badge(candidate.slug) if candidate else None
            allowed_raw = {canonical(candidate.slug), badge[0]} if badge else set()
            if candidate and (not raw or raw.isdigit() or raw in allowed_raw):
                return self.canonical_model(candidate)
        choices = self.model_aliases.get((make.key,canonical(ad.get('model',''))), [])
        unique = {self.canonical_model(node).key:self.canonical_model(node) for node in choices}
        if len(unique) == 1:
            return next(iter(unique.values()))
        if len(unique)>1:
            # Canonical punctuation can collide: Saab 93 and 9-3 are distinct cars.
            # Prefer the longest literal catalogue name in the actual title.
            title=ad.get('name','')
            scores={}
            for make_alias in aliases(make):
                found=prefix(title,make_alias)
                if not found:continue
                tail=title[found.end():].strip()
                for node in unique.values():
                    for value in (node.name,):
                        pattern=re.escape(value).replace(r'\ ',r'\s+')
                        if re.match('^'+pattern+r'(?=\W|$)',tail,re.I):
                            scores[node.key]=max(scores.get(node.key,0),len(value))
            if scores:
                best=[key for key,score in scores.items() if score==max(scores.values())]
                if len(best)==1:return unique[best[0]]
            raw=str(ad.get('model','')).strip().casefold()
            exact={node.key:node for node in unique.values() if raw in (node.slug.casefold(),node.name.casefold())}
            return next(iter(exact.values())) if len(exact)==1 else None
        title = ad.get('name','')
        for make_alias in sorted(aliases(make),key=len,reverse=True):
            make_match = prefix(title,make_alias)
            if not make_match:
                continue
            tail = title[make_match.end():].lstrip(' ()-_')
            for model_alias,model in self.model_prefixes.get(make.key,[]):
                if prefix(tail,model_alias):
                    raw = canonical(ad.get('model',''))
                    # Repair numeric source IDs and engine suffixes, never contradict a model.
                    parts = re.split(r'\s+',make_alias.strip(),maxsplit=1)
                    old_split = canonical(parts[1]) if len(parts)>1 and canonical(ad.get('brand',''))==canonical(parts[0]) else ''
                    repaired = raw[len(old_split):] if old_split and raw.startswith(old_split) else raw
                    if not repaired or (not old_split and raw.isdigit() and len(raw)>=6) or any(repaired.startswith(canonical(a)) for a in aliases(model)):
                        return self.canonical_model(model)
                    return None
        return None

    def label_from_title(self, ad, model):
        if ad.get('platform') != 'autoru':
            return ''
        title = ad.get('name','')
        for make_alias in sorted(aliases(model.parent),key=len,reverse=True):
            make_match = prefix(title,make_alias)
            if not make_match:
                continue
            tail = title[make_match.end():].lstrip(' ()-_')
            for model_alias in sorted(aliases(model),key=len,reverse=True):
                model_match = prefix(tail,model_alias)
                if model_match:
                    suffix = tail[model_match.end():].strip()
                    return re.split(r',\s*(?:19|20)\d{2}\b',suffix,maxsplit=1)[0].strip()
        return title_generation(title)

    def normalize(self, ad, generation_url=None):
        model = self.identify_model(ad)
        nodes = self.by_model.get(model.key,[]) if model else self.unscoped
        if model:
            ad['brand'],ad['model'] = model.parent.slug,model.slug
        previous = ad.get('generation_evidence') or {}
        label = ad.get('generation') or ''
        canonical_labels = {node.name for node in nodes}
        if previous.get('label') and (not label or label in canonical_labels):
            label = previous['label']
        elif not label:
            label = self.label_from_title(ad,model) if model else title_generation(ad.get('name','')) if ad.get('platform') == 'autoru' else ''
        previous_url = previous.get('url') if not previous.get('label') or previous.get('label') == label else ''
        url = generation_url or previous_url or ''
        evidence = {'version':VERSION,'source':ad.get('platform',''),'label':label[:1000],'url':url,
                    'status':'unknown','method':'','candidate_keys':[],'model_key':model.key if model else ''}
        ad['generation_key'] = ''
        ad['generation_evidence'] = evidence
        # Raw source labels survive an ambiguous or conflicting normalization.
        ad['generation'] = label
        ad['generation_codes'] = []
        exact = [node for node in nodes if url and url in node.data.get('generation_urls',[])]
        method = 'catalog_url'
        candidates = exact
        phase = facelift(label)
        order = ordinal(label)
        span = production_range(label)
        phase_known = bool(order or span or phase or re.search(r'дорест|pre.?facelift',label,re.I))
        if not candidates:
            candidates = [node for node in nodes if node.name == label and label]
            method = 'catalog_label'
        if not candidates:
            candidates = [node for node in nodes if any(contains_code(label,code)
                for code in self.codes_for(node, model, phase))]
            method = 'body_code'
        has_unknown_code = bool(re.search(r'\b[A-Za-z]{1,5}\s*\d+[A-Za-z]*\b',re.sub(r'\b(?:19|20)\d{2}\b','',label)))
        source_span = SOURCE_GENERATION_PERIODS.get((model.key,ad.get('platform',''),order,phase)) if model else None
        if not candidates and not has_unknown_code and source_span and (not span or span == source_span):
            # A source family can cover several Drom phases. Retain boundary ambiguity.
            catalogue_span = SOURCE_CATALOGUE_PERIODS.get((model.key, ad.get('platform',''), order, phase), source_span)
            candidates = [node for node in nodes if
                (node.data.get('generation_number') or ordinal(node.data.get('description',''))) == order and
                node.data.get('year_from') is not None and node.data['year_from'] >= catalogue_span[0] and
                (catalogue_span[1] is None or node.data.get('year_to') is not None and node.data['year_to'] <= catalogue_span[1])]
            method = 'source_period'
        if not candidates and span and not has_unknown_code:
            candidates = [node for node in nodes if span == (node.data.get('year_from'),node.data.get('year_to'))
                or any(span == (p.get('year_from'),p.get('year_to')) for p in node.data.get('periods',[]))]
            method = 'label_range'
        # A foreign, explicit body code must not fall back to an unrelated ordinal.
        if not candidates and not span and not has_unknown_code and model and order:
            verified_orders = self.source_ordinals.get((model.key,ad.get('platform',''),order))
            orders = verified_orders or {order}
            candidates = [node for node in nodes if (node.data.get('generation_number') or ordinal(node.data.get('description',''))) in orders]
            method = 'source_ordinal' if verified_orders else 'ordinal'
            if verified_orders and len(verified_orders) > 1:
                evidence.update(status='ambiguous',method=method,candidate_keys=[node.key for node in candidates][:20])
                return ad
        explicit_candidates = candidates
        if method not in ('catalog_url','source_period') and phase_known:
            candidates = [node for node in candidates if facelift(node.data.get('description','')) == phase]
        year = ad.get('year')
        if str(year).isdigit():
            candidates = [node for node in candidates if
                (not node.data.get('year_from') or int(year) >= node.data['year_from']) and
                (not node.data.get('year_to') or int(year) <= node.data['year_to'])]
            if method == 'source_period' and source_span and (int(year) < source_span[0] or
                    source_span[1] is not None and int(year) > source_span[1]):
                candidates = []
        if explicit_candidates and not candidates and method in ('catalog_url','catalog_label','body_code','label_range','source_period'):
            evidence.update(status='conflict',method=method,candidate_keys=[node.key for node in explicit_candidates][:20])
            return ad
        evidence['candidate_keys'] = [node.key for node in candidates][:20]
        if len(candidates) == 1:
            node = candidates[0]
            ad['generation'],ad['generation_codes'],ad['generation_key'] = node.name,node.data.get('codes',[]),node.key
            evidence.update(status='resolved',method=method)
        elif candidates:
            evidence.update(status='ambiguous',method=method)
        return ad


def normalize_generation(ad, generations, generation_url=None):
    index = generations if isinstance(generations,GenerationIndex) else GenerationIndex(generations)
    return index.normalize(ad,generation_url)
