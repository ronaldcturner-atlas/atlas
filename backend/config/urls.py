"""Atlas backend URL configuration."""

from django.urls import include, path

urlpatterns = [
    path("api/", include("apps.accounts.urls")),
    path("api/", include("apps.common.urls")),
    path("api/", include("apps.domains.urls")),
    path("api/", include("apps.facilities.urls")),
    path("api/", include("apps.scheduling.urls")),
]
