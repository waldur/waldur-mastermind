from django.urls import re_path

from . import views

urlpatterns = [
    re_path(
        r"^projects/(?P<uuid>[a-f0-9]+)/sync_user_roles/$",
        views.ProjectSyncUserRolesView.as_view(),
        name="project-sync-user-roles",
    ),
]
