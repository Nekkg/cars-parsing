"""Normalize existing ads in batches while preserving all timestamps and prices."""
import json
from collections import Counter
from django.core.management.base import BaseCommand
from ads.models import Advertisement
from ads.search import canonical
from parsers.generations import GenerationIndex


class Command(BaseCommand):
    help = 'Normalize all stored source evidence against the catalogue; --apply writes changes.'

    def add_arguments(self, parser):
        parser.add_argument('--apply',action='store_true')

    def handle(self, *args, **options):
        index = GenerationIndex.from_db()
        # Learn source-specific numbering only from explicit body codes/URLs/periods.
        # This read-only pass makes dry runs and updates independent of row order.
        index.learn_from_ads(Advertisement.objects.order_by('pk').iterator(chunk_size=500))
        fields = ['generation','generation_codes','generation_key','generation_evidence','brand','model','brand_key','model_key']
        pending, statuses = [], Counter()
        processed = changed = 0
        for row in Advertisement.objects.order_by('pk').iterator(chunk_size=500):
            data = dict(row.__dict__)
            index.normalize(data)
            data['brand_key'], data['model_key'] = canonical(data['brand']),canonical(data['model'])
            processed += 1
            statuses[(row.platform,data['generation_evidence']['status'])] += 1
            if any(getattr(row,field) != data[field] for field in fields):
                changed += 1
                if options['apply']:
                    for field in fields:
                        setattr(row,field,data[field])
                    pending.append(row)
            if len(pending) >= 500:
                Advertisement.objects.bulk_update(pending,fields,batch_size=250)
                pending.clear()
        if pending:
            Advertisement.objects.bulk_update(pending,fields,batch_size=250)
        report = {'processed':processed,'changed':changed,'applied':options['apply'],
                  'coverage':{source:{status:statuses[(source,status)] for status in ('resolved','ambiguous','unknown','conflict')}
                              for source in ('avito','autoru','drom')}}
        self.stdout.write(json.dumps(report,ensure_ascii=False))
