from django.urls import re_path

from waldur_core.structure import views


def register_in(router):
    router.register(r'customers', views.CustomerViewSet)
    router.register(r'project-types', views.ProjectTypeViewSet, basename='project_type')
    router.register(r'projects', views.ProjectViewSet)
    router.register(
        r'customer-permissions',
        views.CustomerPermissionViewSet,
        basename='customer_permission',
    )
    router.register(
        r'customer-permissions-log',
        views.CustomerPermissionLogViewSet,
        basename='customer_permission_log',
    )
    router.register(
        r'customer-permissions-reviews',
        views.CustomerPermissionReviewViewSet,
        basename='customer_permission_review',
    )
    router.register(
        r'project-permissions',
        views.ProjectPermissionViewSet,
        basename='project_permission',
    )
    router.register(
        r'project-permissions-log',
        views.ProjectPermissionLogViewSet,
        basename='project_permission_log',
    )
    router.register(r'service-settings', views.ServiceSettingsViewSet)
    router.register(r'users', views.UserViewSet)
    router.register(r'keys', views.SshKeyViewSet)
    router.register(r'divisions', views.DivisionViewSet, basename='division')
    router.register(
        r'division-types', views.DivisionTypesViewSet, basename='division-type'
    )
    router.register(
        r'user-agreements',
        views.UserAgreementsViewSet,
        basename='user-agreements',
    )
    router.register(
        r'notification-messages',
        views.NotificationViewSet,
        basename='notification-messages',
    )
    router.register(
        r'notification-messages-templates',
        views.NotificationTemplateViewSet,
        basename='notification-messages-templates',
    )


urlpatterns = [
    re_path(
        r'^customers/(?P<uuid>[a-f0-9]+)/counters/$',
        views.CustomerCountersView.as_view({'get': 'list'}),
        name='customer_counters',
    ),
    re_path(
        r'^projects/(?P<uuid>[a-f0-9]+)/counters/$',
        views.ProjectCountersView.as_view({'get': 'list'}),
        name='project_counters',
    ),
]
