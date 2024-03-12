from . import views


def register_in(router):
    router.register(
        r"daily-quotas", views.DailyQuotaHistoryViewSet, basename="daily-quotas"
    )
    router.register(
        r"project-quotas", views.ProjectQuotasViewSet, basename="project-quotas"
    )
    router.register(
        r"customer-quotas", views.CustomerQuotasViewSet, basename="customer-quotas"
    )
