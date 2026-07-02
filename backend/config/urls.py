"""Atlas backend URL configuration."""

from django.urls import include, path

urlpatterns = [
    path("api/", include("apps.common.urls")),
    path("api/", include("apps.scheduling.urls")),
]
