from __future__ import unicode_literals

from waldur_mastermind.marketplace import views


def register_in(router):
    router.register(r'marketplace-service-providers', views.ServiceProviderViewSet,
                    base_name='marketplace-service-provider')
