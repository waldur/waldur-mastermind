from __future__ import unicode_literals

from . import views


def register_in(router):
    router.register(r'daily-quotas', views.DailyQuotaHistoryViewSet, base_name='daily-quotas')
