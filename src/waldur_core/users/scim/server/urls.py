"""URL configuration for the inbound SCIM 2.0 service provider.

Mounted at ``/scim/v2/`` (see ``waldur_core/server/urls.py``). All paths are
intentionally outside ``/api/`` so they're excluded from the main OpenAPI schema
(``SCHEMA_PATH_PREFIX="/api/"``) and follow RFC 7644 conventions.
"""

from django.urls import path

from waldur_core.users.scim.server import groups_view, users_view, views

urlpatterns = [
    path("ServiceProviderConfig", views.ServiceProviderConfigView.as_view()),
    path("ResourceTypes", views.ResourceTypesView.as_view()),
    path("ResourceTypes/<str:name>", views.ResourceTypeDetailView.as_view()),
    path("Schemas", views.SchemasView.as_view()),
    path("Schemas/<path:urn>", views.SchemaDetailView.as_view()),
    path("Users", users_view.UsersListView.as_view()),
    path("Users/<str:uuid_hex>", users_view.UserDetailView.as_view()),
    path("Groups", groups_view.GroupsListView.as_view()),
    path("Groups/<path:group_id>", groups_view.GroupDetailView.as_view()),
]
