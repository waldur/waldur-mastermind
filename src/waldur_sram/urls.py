"""SRAM SCIM endpoints, mounted at ``/scim/v2/sram/`` by the extension."""

from django.urls import path, re_path

from waldur_core.users.scim.server import views as scim_views

from . import views

urlpatterns = [
    path("ServiceProviderConfig", scim_views.ServiceProviderConfigView.as_view()),
    path("ResourceTypes", scim_views.ResourceTypesView.as_view()),
    path("ResourceTypes/<str:name>", scim_views.ResourceTypeDetailView.as_view()),
    path("Schemas", scim_views.SchemasView.as_view()),
    path("Schemas/<path:urn>", scim_views.SchemaDetailView.as_view()),
    path("Users", views.UsersListView.as_view()),
    path("Users/<str:uuid_hex>", views.UserDetailView.as_view()),
    path("Groups", views.GroupsListView.as_view()),
    path("Groups/<str:uuid_hex>", views.GroupDetailView.as_view()),
    re_path(r"^.*$", scim_views.ScimNotFoundView.as_view()),
]
