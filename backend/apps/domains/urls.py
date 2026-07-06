from django.urls import path

from . import api

urlpatterns = [
    path('domains/', api.domains_list_create, name='domains_list_create'),
    path('domains/<int:domain_id>/', api.domain_detail, name='domain_detail'),
]
