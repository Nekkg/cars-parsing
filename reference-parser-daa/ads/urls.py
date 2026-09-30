from django.urls import path
from . import views

app_name = 'ads'

urlpatterns = [
    path('catalog/', views.catalog_options, name='catalog'),
    path('', views.index, name='index'),
    path('ad/<str:ad_id>/', views.ad_detail, name='ad_detail'),
] 
