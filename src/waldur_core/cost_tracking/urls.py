from __future__ import unicode_literals

from waldur_core.cost_tracking import views


def register_in(router):
    router.register(r'price-estimates', views.PriceEstimateViewSet)
    router.register(r'default-price-list-items', views.DefaultPriceListItemViewSet)
    router.register(r'service-price-list-items', views.PriceListItemViewSet)
    router.register(r'merged-price-list-items', views.MergedPriceListItemViewSet, base_name='merged-price-list-item')
