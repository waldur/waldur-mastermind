from __future__ import unicode_literals

from waldur_mastermind.experts import views


def register_in(router):
    router.register(r'expert-providers', views.ExpertProviderViewSet, base_name='expert-provider')
    router.register(r'expert-requests', views.ExpertRequestViewSet, base_name='expert-request')
    router.register(r'expert-bids', views.ExpertBidViewSet, base_name='expert-bid')
