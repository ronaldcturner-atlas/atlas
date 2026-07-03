from django.urls import path

from . import api

urlpatterns = [
    path("login/", api.login_view, name="login"),
    path("logout/", api.logout_view, name="logout"),
    path("me/", api.me_view, name="me"),
    path("physicians/", api.physicians_list_create, name="physicians_list_create"),
    path("physicians/<int:physician_id>/", api.physician_detail, name="physician_detail"),
    path("physicians/<int:physician_id>/disable/", api.physician_disable, name="physician_disable"),
]
