from django.urls import path

from . import api

urlpatterns = [
    path("login/", api.login_view, name="login"),
    path("logout/", api.logout_view, name="logout"),
    path("me/", api.me_view, name="me"),
]
