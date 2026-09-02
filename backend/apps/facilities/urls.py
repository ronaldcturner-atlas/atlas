from django.urls import path

from . import api

urlpatterns = [
    path('facilities/', api.facilities_list_create, name='facilities_list_create'),
    path('facilities/reorder/', api.facilities_reorder, name='facilities_reorder'),
    path('facilities/<int:facility_id>/', api.facility_detail, name='facility_detail'),
    path('facilities/<int:facility_id>/disable/', api.facility_disable, name='facility_disable'),
]
