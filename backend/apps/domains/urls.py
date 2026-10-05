from django.urls import path

from . import api

urlpatterns = [
    path('organizations/', api.organizations_list_create, name='organizations_list_create'),
    path('organizations/<int:organization_id>/', api.organization_detail, name='organization_detail'),
    path('organizations/<int:organization_id>/memberships/', api.organization_memberships, name='organization_memberships'),
    path('organizations/<int:organization_id>/regions/', api.regions_list_create, name='regions_list_create'),
    path('regions/<int:region_id>/', api.region_detail, name='region_detail'),
    path('domains/', api.domains_list_create, name='domains_list_create'),
    path('domains/<int:domain_id>/', api.domain_detail, name='domain_detail'),
    path('domains/<int:domain_id>/memberships/', api.domain_memberships, name='domain_memberships'),
    path('domain-memberships/<int:membership_id>/', api.domain_membership_detail, name='domain_membership_detail'),
]
