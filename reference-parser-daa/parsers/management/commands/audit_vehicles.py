import csv
import json
from collections import Counter
from uuid import uuid4
from django.utils import timezone
from django.core.management.base import BaseCommand
from ads.models import CatalogNode
from ads.catalog_search import POPULAR,ranked_rows,normalize
from parsers.models import VehicleAudit
from parsers.vehicle_audit import query_for
from parsers.audit_scope import POPULAR_MODELS,read_scope,save_scope
from parsers.sources import source_url
from core.paths import RUNTIME_DIR


def spreadsheet_rows(records):
    for record in records:
        # Public ad titles are untrusted; never let Excel interpret them as formulas.
        yield {key:("'"+value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value)
            for key,value in record.items()}


class Command(BaseCommand):
    help='Prepare/resume one real ad check per catalogue model and source; export evidence as CSV/JSON.'

    def add_arguments(self,parser):
        parser.add_argument('--prepare',action='store_true')
        parser.add_argument('--report',action='store_true')
        parser.add_argument('--retry-failed',action='store_true')
        scope=parser.add_mutually_exclusive_group()
        scope.add_argument('--popular',action='store_true')
        scope.add_argument('--all-models',action='store_true')

    def handle(self,*args,**options):
        scope=read_scope()
        if options.get('popular'):
            desired=[make+'/'+model for make,models in POPULAR_MODELS.items() for model in models]
            found=set(CatalogNode.objects.filter(kind='model',key__in=desired).values_list('key',flat=True))
            scope={'kind':'popular','model_keys':[key for key in desired if key in found],
                'unresolved_model_keys':[key for key in desired if key not in found]}
            save_scope(scope)
            VehicleAudit.objects.filter(status='pending').exclude(model__key__in=found).update(status='deferred')
            VehicleAudit.objects.filter(status='deferred',model__key__in=found).update(status='pending')
        elif options.get('all_models'):
            scope={'kind':'all'};save_scope(scope)
            VehicleAudit.objects.filter(status='deferred').update(status='pending')
        keys=scope.get('model_keys') if scope.get('kind')=='popular' else None
        if options['prepare']:
            makes=list(CatalogNode.objects.filter(kind='make'))
            models_qs=CatalogNode.objects.filter(kind='model')
            if keys is not None:models_qs=models_qs.filter(key__in=keys)
            models=list(models_qs.select_related('parent'))
            priorities={slug:i for i,slug in enumerate(POPULAR)}
            makes.sort(key=lambda m:(priorities.get(normalize(m.slug),999),m.name))
            planned=[]
            for make in makes:
                children=ranked_rows([m for m in models if m.parent_id==make.pk],make_slug=make.slug)
                for model in children:
                    for source in ('avito','autoru','drom'):planned.append(VehicleAudit(model=model,source=source))
            VehicleAudit.objects.bulk_create(planned,ignore_conflicts=True,batch_size=500)
        audits=VehicleAudit.objects.all()
        if keys is not None:audits=audits.filter(model__key__in=keys)
        if options['retry_failed']:
            audits.exclude(status__in=('confirmed','empty','pending','deferred')).update(status='pending',lease_until=None)
        rows=list(audits.select_related('model__parent').order_by('pk'))
        summary={'generated_at':timezone.now().isoformat(),'scope':scope.get('kind','all'),'models':len({row.model_id for row in rows}),'catalog_models':CatalogNode.objects.filter(kind='model').count(),'unresolved_model_keys':scope.get('unresolved_model_keys',[]),'makes_without_models':list(CatalogNode.objects.filter(kind='make',children__isnull=True).values_list('key',flat=True)),'total':len(rows),'states':dict(Counter(row.status for row in rows)),
            'sources':{source:dict(Counter(row.status for row in rows if row.source==source)) for source in ('avito','autoru','drom')}}
        if options['report']:
            directory=RUNTIME_DIR/'reports';directory.mkdir(parents=True,exist_ok=True)
            records=[]
            for row in rows:
                records.append({'make':row.model.parent.name,'model':row.model.name,'model_key':row.model.key,
                    'source':row.source,'status':row.status,'checked_at':row.checked_at.isoformat() if row.checked_at else '',
                    'query_url':row.evidence.get('query_url') or source_url(row.source,query_for(row.model)),
                    **row.evidence})
            token=uuid4().hex
            temp=directory/('vehicle-audit-'+token+'.json.tmp')
            temp.write_text(json.dumps({'summary':summary,'checks':records},ensure_ascii=False,indent=2),encoding='utf-8')
            temp.replace(directory/'vehicle-audit.json')
            fields=('make','model','model_key','source','status','checked_at','ad_url','title','price','photos','photo_checked','query_url','error_type')
            csv_temp=directory/('vehicle-audit-'+token+'.csv.tmp')
            with csv_temp.open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(spreadsheet_rows(records))
            csv_temp.replace(directory/'vehicle-audit.csv')
            matrix={}
            for record in records:
                item=matrix.setdefault(record['model_key'],{'make':record['make'],'model':record['model'],'model_key':record['model_key']})
                source=record['source'];item[source]=record['status'];item[source+'_url']=record.get('ad_url','')
            matrix_temp=directory/('vehicle-audit-by-model-'+token+'.csv.tmp')
            fields=('make','model','model_key','avito','avito_url','autoru','autoru_url','drom','drom_url')
            with matrix_temp.open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(spreadsheet_rows(matrix.values()))
            matrix_temp.replace(directory/'vehicle-audit-by-model.csv')
        self.stdout.write(json.dumps(summary,ensure_ascii=False))