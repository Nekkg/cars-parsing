"""Avito's explicit RAV4 facelift family covers regional catalogue phases."""
from django.test import TestCase
from ads.models import CatalogNode
from parsers.generations import GenerationIndex


class Rav4SourceFamilyTests(TestCase):
    def setUp(self):
        make = CatalogNode.objects.create(kind='make',key='toyota',slug='toyota',name='Toyota')
        model = CatalogNode.objects.create(kind='model',key='toyota/rav4',slug='rav4',name='RAV4',parent=make)
        self.nodes = []
        for key,start,end,phase in (('base',2005,2008,0),('rest',2008,2016,1),('rest2',2010,2013,2)):
            self.nodes.append(CatalogNode.objects.create(kind='generation',parent=model,key='toyota/rav4/'+key,
                name='RAV4 '+key,data={'year_from':start,'year_to':end,'generation_number':3,
                'description':'3 поколение'+(', '+str(phase)+'-й рестайлинг' if phase else ''),'codes':['XA30']}))
        self.index = GenerationIndex.from_db()

    def normalize(self, year, label='III рестайлинг (2009—2016)'):
        ad = dict(platform='avito',brand='toyota',model='rav4',year=year,generation=label,name='Toyota RAV4')
        self.index.normalize(ad)
        return ad

    def test_source_family_resolves_when_only_one_catalogue_phase_fits(self):
        ad = self.normalize('2015')
        self.assertEqual(ad['generation_key'], self.nodes[1].key)
        self.assertEqual(ad['generation_evidence']['method'], 'source_period')

    def test_overlapping_regional_facelifts_remain_ambiguous(self):
        ad = self.normalize('2010')
        self.assertEqual(ad['generation_key'], '')
        self.assertEqual(ad['generation_evidence']['status'], 'ambiguous')
        self.assertEqual(set(ad['generation_evidence']['candidate_keys']), {n.key for n in self.nodes[1:]})

    def test_unknown_label_and_year_alone_never_identify_a_generation(self):
        for label in ('', 'III рестайлинг (ZZ99)', 'II рестайлинг (2009—2016)'):
            self.assertEqual(self.normalize('2015',label)['generation_key'], '')

    def test_year_outside_explicit_source_period_does_not_resolve(self):
        for year in ('2008','2017'):
            self.assertEqual(self.normalize(year)['generation_key'], '')
