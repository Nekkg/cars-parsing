"""Offline consistency of all catalogue inputs; never counts as live ad evidence."""
import json
from collections import Counter
from urllib.parse import urlparse
from django.core.management.base import BaseCommand
from ads.models import CatalogNode
from parsers.generations import GenerationIndex
from parsers.vehicle_audit import query_for
from parsers.sources import source_url
from parsers.avito_client import vehicle_name
from core.paths import RUNTIME_DIR


class Command(BaseCommand):
    help='Check recognition of every catalogue make/model, without requests or changing ads.'

    def handle(self,*args,**options):
        index=GenerationIndex.from_db()
        models=list(CatalogNode.objects.filter(kind='model').select_related('parent'))
        failures=[]
        for row in models:
            filters=query_for(row);title=row.parent.name+' '+row.name+', 2020'
            for source in ('avito','autoru','drom'):
                if source=='avito':brand,model=vehicle_name(title)
                elif source=='autoru':
                    parts=urlparse(source_url(source,filters)).path.strip('/').split('/')
                    brand,model=parts[-3:-1]
                else:brand,model=row.parent.slug,row.slug
                ad={'brand':brand,'model':model,'platform':source,'name':title,'year':'2020'}
                actual=index.identify_model(ad);expected=index.canonical_model(row)
                if actual is None or actual.key!=expected.key:
                    failures.append({'model_key':row.key,'source':source,'brand_input':brand,
                        'model_input':model,'recognized':actual.key if actual else None})
        summary={'scope':'OFFLINE synthetic catalogue inputs, NOT live ad confirmations',
            'models':len(models),'checks':len(models)*3,'failures':len(failures),
            'failures_by_source':dict(Counter(row['source'] for row in failures))}
        directory=RUNTIME_DIR/'reports';directory.mkdir(parents=True,exist_ok=True)
        (directory/'catalogue-consistency.json').write_text(json.dumps({**summary,'details':failures},
            ensure_ascii=False,indent=2),encoding='utf-8')
        self.stdout.write(json.dumps(summary,ensure_ascii=False))