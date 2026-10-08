from django.urls import path

from . import api

urlpatterns = [
    path("csrf/", api.csrf_view, name="csrf"),
    path("login/", api.login_view, name="login"),
    path("logout/", api.logout_view, name="logout"),
    path("me/", api.me_view, name="me"),
    path("password/change/", api.change_password, name="change_password"),
    path("development/role-test/", api.development_role_test, name="development_role_test"),
    path("user-directory/", api.user_directory, name="user_directory"),
    path("physicians/", api.physicians_list_create, name="physicians_list_create"),
    path("physicians/<int:physician_id>/", api.physician_detail, name="physician_detail"),
    path("physicians/<int:physician_id>/disable/", api.physician_disable, name="physician_disable"),
    path("physicians/<int:physician_id>/password-reset/", api.physician_reset_password, name="physician_reset_password"),
]
