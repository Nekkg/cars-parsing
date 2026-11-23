from django.urls import path
from . import views
app_name = 'ads'
urlpatterns = [
    path('', views.index, name='index'),
    path('catalog/', views.catalog_options, name='catalog'),
    path('api/cities/', views.city_options, name='cities'),
    path('api/feed/', views.stored_feed, name='stored_feed'),
    path('photos/<int:pk>/<int:index>/<str:size>/', views.photo, name='photo'),
    path('api/query/', views.parse_query, name='parse_query'),
    path('api/searches/', views.create_search, name='create_search'),
    path('api/searches/<uuid:search_id>/', views.search_results, name='search_results'),
    path('api/searches/<uuid:search_id>/refresh/', views.refresh_search, name='refresh_search'),
    path('ad/<str:ad_id>/', views.ad_detail, name='ad_detail'),
]
