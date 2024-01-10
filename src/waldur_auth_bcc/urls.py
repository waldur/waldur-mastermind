from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r"^api-auth/bcc/user-details/$", views.UserDetailsViewSet.as_view())
]
