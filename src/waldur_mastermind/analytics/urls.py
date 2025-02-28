from django.urls import re_path

from . import views


def register_in(router):
    router.register(
        r"project-quotas", views.ProjectQuotasViewSet, basename="project-quotas"
    )
    router.register(
        r"customer-quotas", views.CustomerQuotasViewSet, basename="customer-quotas"
    )


urlpatterns = [
    re_path(
        r"^api/daily-quotas/",
        views.DailyQuotaHistoryViewSet.as_view(),
        name="daily-quotas-list",
    ),
]
