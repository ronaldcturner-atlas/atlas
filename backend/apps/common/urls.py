from django.urls import path

from . import api

urlpatterns = [
    path("health/", api.health, name="health"),
    path("ready/", api.readiness, name="readiness"),
]
