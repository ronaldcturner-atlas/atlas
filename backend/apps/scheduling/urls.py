from django.urls import path

from . import api

urlpatterns = [
    path("shifts/", api.shifts_list, name="shifts_list"),
]
