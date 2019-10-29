from . import views


def register_in(router):
    router.register(r'daily-quotas', views.DailyQuotaHistoryViewSet, basename='daily-quotas')
