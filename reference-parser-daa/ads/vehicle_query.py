"""Shared Russian vehicle resolution for Telegram and the website."""
from .catalog import load_children
from .catalog_search import normalize
from .models import CatalogNode
from .text_query import model_suggestions
from .query_language import parse_request, model_aliases

def resolve_query(text):
    from parsers.generations import contains_code
    parsed = parse_request(text,load_children())
    make = parsed.pop('make')
    model_text = parsed.pop('model_text')
    variant,hint = '',''
    known_expression = True
    if not make:
        # Model-only queries use exact names/aliases, never infer a make from fuzzy matches.
        candidates = [node for node in CatalogNode.objects.filter(kind='model').select_related('parent')
                      if normalize(model_text) in {normalize(alias) for alias in model_aliases(node)}]
        if not candidates: return None
        make = candidates[0].parent
        models = candidates
    elif not model_text:
        models = []
    else:
        # A known chassis code identifies its model. Facelifts remain in the same family.
        codes = []
        for token in model_text.split():
            if not any(c.isdigit() for c in token): continue
            rows = [node for node in CatalogNode.objects.filter(kind='generation',parent__parent=make).select_related('parent')
                    if any(contains_code(token,code) for code in (*node.data.get('codes',[]),*node.data.get('frame_codes',[])))]
            if rows: codes.append((token,rows))
        if len(codes)>1: raise ValueError('Укажите один код кузова или выберите поколение кнопками.')
        if codes:
            hint,rows = codes[0]
            hint = hint.upper()
            models = list({node.parent_id:node.parent for node in rows}.values())
            remainder = ' '.join(word for word in model_text.split() if word.upper()!=hint)
            if remainder:
                other,variant = model_suggestions(make,remainder,load_children(make.pk))
                models = [node for node in models if node.pk in {row.pk for row in other}]
                if not models: raise ValueError('Модель и код кузова не совпадают. Проверьте запрос.')
        else:
            models,variant = model_suggestions(make,model_text,load_children(make.pk))
            known_expression = bool(variant) or any(normalize(model_text) in {normalize(alias) for alias in model_aliases(model)} for model in models)
            if normalize(make.slug)=='bmw' and model_text in tuple('12345678'): known_expression=True
    parsed['generation_hint'] = hint
    return {'make_id':make.pk,'make_name':make.name,
            'models':[(node.pk,(node.parent.name+' ' if node.parent_id!=make.pk else '')+node.name) for node in models],
            'year':parsed['year'],'variant':variant,'model_text':model_text,'extra':parsed,'needs_confirmation':not known_expression}



