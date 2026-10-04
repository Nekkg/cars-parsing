"""Populate catalogue generations for observed cars, or explicitly for all makes."""
import time
from collections import Counter
from django.core.management.base import BaseCommand, CommandError
from ads.models import Advertisement, CatalogNode
from ads.catalog import load_children
from ads.search import canonical
from parsers.generations import GenerationIndex


class Command(BaseCommand):
    help = 'Load generations for all observed vehicles; --all loads the complete catalogue.'

    def add_arguments(self, parser):
        parser.add_argument('--all',action='store_true')
        parser.add_argument('--make')
        parser.add_argument('--model')
        parser.add_argument('--force',action='store_true')
        parser.add_argument('--delay',type=float,default=0.5)

    def handle(self,*args,**options):
        delay = max(0.25,min(options['delay'],10))
        index = GenerationIndex.from_db()
        if not index.makes:
            load_children()
            index = GenerationIndex.from_db()
        ads = list(Advertisement.objects.order_by().values('brand','model','name'))
        counts = Counter()
        for ad in ads:
            make = index.identify_make(ad)
            if make:
                counts[make.key] += 1
        if options['all']:
            counts = Counter({node.key:1 for node in index.makes.values()})
        if options['make']:
            make_keys = {node.key for node in index.make_aliases.get(canonical(options['make']),[])}
            if not make_keys:
                raise CommandError('Make not found in catalogue')
            counts = Counter({key:max(1,counts[key]) for key in make_keys})
        errors = 0
        for key,_ in counts.most_common():
            make = index.makes[key]
            try:
                load_children(make.pk,force=options['force'])
            except Exception as error:
                errors += 1
                self.stdout.write(f'Make {key}: {type(error).__name__}')
            time.sleep(delay)
        index = GenerationIndex.from_db()
        targets = Counter()
        for ad in ads:
            model = index.identify_model(ad)
            if model and model.parent.key in counts:
                targets[model.key] += 1
        if options['all'] or options['make'] and options['model']:
            targets = Counter({node.key:1 for node in index.models.values() if node.parent.key in counts})
        if options['model']:
            targets = Counter({key:n for key,n in targets.items() if canonical(options['model']) in
                               {canonical(index.models[key].slug),canonical(index.models[key].name)}})
        self.stdout.write(f'Target makes: {len(counts)}; models: {len(targets)}')
        loaded = 0
        for key,_ in targets.most_common():
            node = index.models[key]
            old = list(CatalogNode.objects.filter(kind='generation',parent=node))
            enriched = bool(old) and all(gen.data.get('schema_version')==2 for gen in old)
            try:
                generations = load_children(node.pk,force=options['force'] or not enriched)
                if not generations or any(gen.data.get('schema_version') != 2 for gen in generations):
                    errors += 1
                else:
                    loaded += 1
            except Exception as error:
                errors += 1
                self.stdout.write(f'Model {key}: {type(error).__name__}')
            if loaded % 25 == 0:
                self.stdout.write(f'Loaded models: {loaded}/{len(targets)}; errors: {errors}')
            time.sleep(delay)
        self.stdout.write(f'Catalogue finished: models={loaded}/{len(targets)}; errors={errors}; generations={CatalogNode.objects.filter(kind="generation").count()}')
