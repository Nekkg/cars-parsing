from django.test import SimpleTestCase
from parsers.avito_client import vehicle_name
from parsers.sources import extract_listings

class AvitoMarkupTests(SimpleTestCase):
 def test_model_uses_catalog_name_without_engine(self):
  self.assertEqual(vehicle_name('BMW 3 \u0441\u0435\u0440\u0438\u044f 2.0 AT, 2018, 100 000 km'),('BMW','3-series'))
  self.assertEqual(vehicle_name('Land Rover Range Rover 3.0 AT, 2020'),('Land Rover','Range Rover'))
 def test_placeholder_is_not_the_car_photo(self):
  html='<div data-marker="item"><a data-marker="item-title" href="/samara/avtomobili/bmw_3_123456789">BMW 3 \u0441\u0435\u0440\u0438\u044f 2.0 AT, 2018</a><span data-marker="item-price">900000</span><img src="/loading.svg"><img src="https://10.img.avito.st/image/car.jpg"></div>'
  a=extract_listings(html,'avito')[0];self.assertEqual(a['model'],'3-series');self.assertEqual(a['image'],'https://10.img.avito.st/image/car.jpg')
 def test_only_placeholder_means_no_photo(self):
  html='<div data-marker="item"><a data-marker="item-title" href="/samara/avtomobili/bmw_x5_123456789">BMW X5, 2020</a><span data-marker="item-price">900000</span><img src="/loading.svg"></div>'
  self.assertEqual(extract_listings(html,'avito')[0]['image'],'')

 def test_detail_gallery_recovers_missing_photo_without_using_site_icon(self):
  from parsers.sources import enrich
  ad={'platform':'avito','name':'BMW X1, 2018','city':'','image':''}
  html='<meta property="og:image" content="https://m.avito.ru/icon.png"><div data-marker="item-view/main-gallery"><img src="/loading.svg"><img src="https://10.img.avito.st/image/car.jpg"></div>'
  self.assertEqual(enrich(ad,html)['image'],'https://10.img.avito.st/image/car.jpg')

from django.test import TestCase
from ads.models import SearchJob
from users.models import UserFilters
from parsers.collector import active_search_jobs


class ActiveSearchQueueTests(TestCase):
 def test_replacing_user_search_stops_old_background_collection(self):
  old=SearchJob.objects.create(fingerprint='old',filters={})
  new=SearchJob.objects.create(fingerprint='new',filters={})
  user=UserFilters.objects.create(user_id=101,search_job=old)
  self.assertEqual(list(active_search_jobs().values_list('pk',flat=True)),[old.pk])
  user.search_job=new;user.save(update_fields=['search_job'])
  self.assertEqual(list(active_search_jobs().values_list('pk',flat=True)),[new.pk])
 def test_shared_search_is_collected_once(self):
  job=SearchJob.objects.create(fingerprint='shared',filters={})
  UserFilters.objects.create(user_id=101,search_job=job)
  UserFilters.objects.create(user_id=102,search_job=job)
  self.assertEqual(active_search_jobs().count(),1)

from parsers.collector import save_ad
from ads.models import Advertisement


class AvitoIdentityTests(TestCase):
 def test_changed_url_updates_same_source_item(self):
  ad={'platform':'avito','external_id':'123456789','link':'https://www.avito.ru/samara/avtomobili/old_123456789','name':'BMW X1','price':1000000}
  save_ad(ad)
  changed={**ad,'link':'https://www.avito.ru/samara/avtomobili/new_123456789','price':950000}
  save_ad(changed)
  self.assertEqual(Advertisement.objects.count(),1)
  row=Advertisement.objects.get(ad_id='avito:123456789')
  self.assertEqual(row.link,changed['link']);self.assertEqual(row.price,950000)
