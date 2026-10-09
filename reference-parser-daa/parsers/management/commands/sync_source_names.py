"""Build exact model names/routes from official public catalogue links."""
import json
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
from urllib.request import Request
from urllib.parse import urlparse,urljoin
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from ads.catalog import load_children
from ads.models import CatalogNode
from parsers.generations import aliases
from parsers.source_names import REGISTRY
from core.network import source_opener


def key(value):
    value=unicodedata.normalize('NFKD',str(value)).casefold()
    return re.sub(r'[^a-z0-9а-я]','',value.replace('ё','е').replace('+','plus'))


def catalogue_links(url,depth):
    with source_opener().open(Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=20) as response:
        html=response.read(8*1024*1024).decode('utf-8',errors='replace')
    soup=BeautifulSoup(html,'html.parser')
    from parsers.sources import check_block
    check_block(soup,'autoru')
    links={}
    for link in soup.find_all('a',href=True):
        parsed=urlparse(urljoin(url,link['href']))
        parts=parsed.path.strip('/').split('/')
        if parsed.scheme=='https' and parsed.hostname=='auto.ru' and len(parts)==depth and parts[:2]==['catalog','cars']:
            name=link.get_text(' ',strip=True)
            if name:links[parsed.path]={'name':name,'slug':parts[-1],'url':'https://auto.ru'+parsed.path}
    if not links:raise ValueError('No official catalogue links')
    return list(links.values())


class Command(BaseCommand):
    help='Load remaining Drom models and synchronize official Auto.ru routes, without changing ads.'

    def add_arguments(self,parser):
        parser.add_argument('--expand-catalog',action='store_true')

    def handle(self,*args,**options):
        errors=[]
        if options['expand_catalog']:
            missing=list(CatalogNode.objects.filter(kind='make',children__isnull=True))
            def load(make):
                close_old_connections()
                try:
                    rows=load_children(make.pk)
                    return make.key,len(rows),''
                except Exception as error:return make.key,0,type(error).__name__
                finally:
                    close_old_connections();time.sleep(.5)
            with ThreadPoolExecutor(max_workers=2) as pool:
                for count,result in enumerate(pool.map(load,missing),1):
                    if result[2]:errors.append({'stage':'drom_models','key':result[0],'error':result[2]})
                    if count%20==0:self.stdout.write('Drom catalogues '+str(count)+'/'+str(len(missing)))
        makes=list(CatalogNode.objects.filter(kind='make'))
        models=list(CatalogNode.objects.filter(kind='model').select_related('parent'))
        by_make=defaultdict(list)
        for model in models:by_make[model.parent.key].append(model)
        official=catalogue_links('https://auto.ru/catalog/cars/',3)
        output={'schema':1,'generated_at':timezone.now().isoformat(),'make_names':[make.name for make in makes],'makes':{},'models':{},'errors':errors,'unmapped_models':[]}
        plans=[]
        for make in makes:
            names={key(value) for value in aliases(make)}
            found=[entry for entry in official if {key(entry['name']),key(entry['slug'])}&names]
            if len(found)!=1:continue
            entry=found[0]
            output['makes'][make.key]={'autoru':{'make_slug':entry['slug'],'aliases':[entry['name'],entry['slug']],'catalog_url':entry['url']}}
            plans.append((make,entry))
        def read(plan):
            make,entry=plan
            try:return make,entry,catalogue_links(entry['url'],4),''
            except Exception as error:return make,entry,[],type(error).__name__
            finally:time.sleep(.5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            for count,(make,entry,source_models,error) in enumerate(pool.map(read,plans),1):
                if error:
                    errors.append({'stage':'autoru_models','key':make.key,'error':error});continue
                lookup=defaultdict(list)
                for model in by_make[make.key]:
                    for name in {key(value) for value in aliases(model)}:lookup[name].append(model)
                for source_model in source_models:
                    display=source_model['name']
                    for brand in sorted({make.name,entry['name'],*aliases(make)},key=len,reverse=True):
                        if display.casefold().startswith(brand.casefold()+' '):
                            display=display[len(brand):].strip();break
                    choices={model.key:model for name in (display,source_model['slug']) for model in lookup.get(key(name),[])}
                    if len(choices)!=1:continue
                    model=next(iter(choices.values()))
                    if model.key in output['models']:
                        # Two source model sections cannot be collapsed by coincidence.
                        output['unmapped_models'].append(model.key)
                        output['models'].pop(model.key,None)
                        continue
                    if model.key in output['unmapped_models']:continue
                    output['models'][model.key]={'autoru':{'make_slug':entry['slug'],'model_slug':source_model['slug'],
                        'aliases':[display,source_model['slug']],'catalog_url':source_model['url']}}
                if count%20==0:self.stdout.write('Auto.ru catalogues '+str(count)+'/'+str(len(plans)))
        REGISTRY.parent.mkdir(parents=True,exist_ok=True)
        temp=REGISTRY.with_suffix('.json.tmp')
        temp.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(REGISTRY)
        self.stdout.write(json.dumps({'makes':len(makes),'models':len(models),'autoru_makes_bound':len(output['makes']),
            'autoru_models_bound':len(output['models']),'errors':len(errors)},ensure_ascii=False))