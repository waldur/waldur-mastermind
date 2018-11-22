from __future__ import unicode_literals

from django.conf.urls import url

from waldur_mastermind.marketplace import views


def register_in(router):
    router.register(r'marketplace-service-providers', views.ServiceProviderViewSet,
                    base_name='marketplace-service-provider'),
    router.register(r'marketplace-categories', views.CategoryViewSet,
                    base_name='marketplace-category'),
    router.register(r'marketplace-offerings', views.OfferingViewSet,
                    base_name='marketplace-offering')
    router.register(r'marketplace-plans', views.PlanViewSet,
                    base_name='marketplace-plan')
    router.register(r'marketplace-screenshots', views.ScreenshotViewSet,
                    base_name='marketplace-screenshot')
    router.register(r'marketplace-cart-items', views.CartItemViewSet,
                    base_name='marketplace-cart-item')
    router.register(r'marketplace-orders', views.OrderViewSet,
                    base_name='marketplace-order')
    router.register(r'marketplace-order-items', views.OrderItemViewSet,
                    base_name='marketplace-order-item')
    router.register(r'marketplace-resources', views.ResourceViewSet,
                    base_name='marketplace-resource')
    router.register(r'marketplace-public-api', views.MarketplaceAPIViewSet,
                    base_name='marketplace-public-api'),


urlpatterns = [
    url(r'^api/customers/(?P<uuid>[^/.]+)/offerings/$', views.CustomerOfferingViewSet.as_view()),
    url(r'^api/marketplace-plugins/$', views.PluginViewSet.as_view()),
]
