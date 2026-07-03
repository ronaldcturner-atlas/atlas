from django.urls import path

from . import api

urlpatterns = [
    path("shifts/", api.shifts_list_create, name="shifts_list_create"),
    path("shifts/<int:shift_id>/", api.shift_detail, name="shift_detail"),
    path("shift-templates/", api.shift_templates_list_create, name="shift_templates_list_create"),
    path("shift-templates/<int:template_id>/", api.shift_template_detail, name="shift_template_detail"),
]
