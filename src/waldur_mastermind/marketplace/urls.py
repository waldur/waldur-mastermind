from django.conf.urls import url

from waldur_mastermind.marketplace import views


def register_in(router):
    router.register(r'marketplace-service-providers', views.ServiceProviderViewSet,
                    basename='marketplace-service-provider'),
    router.register(r'marketplace-categories', views.CategoryViewSet,
                    basename='marketplace-category'),
    router.register(r'marketplace-offerings', views.OfferingViewSet,
                    basename='marketplace-offering')
    router.register(r'marketplace-plans', views.PlanViewSet,
                    basename='marketplace-plan')
    router.register(r'marketplace-screenshots', views.ScreenshotViewSet,
                    basename='marketplace-screenshot')
    router.register(r'marketplace-cart-items', views.CartItemViewSet,
                    basename='marketplace-cart-item')
    router.register(r'marketplace-orders', views.OrderViewSet,
                    basename='marketplace-order')
    router.register(r'marketplace-order-items', views.OrderItemViewSet,
                    basename='marketplace-order-item')
    router.register(r'marketplace-resources', views.ResourceViewSet,
                    basename='marketplace-resource')
    router.register(r'marketplace-category-component-usages', views.CategoryComponentUsageViewSet,
                    basename='marketplace-category-component-usage')
    router.register(r'marketplace-component-usages', views.ComponentUsageViewSet,
                    basename='marketplace-component-usage')
    router.register(r'marketplace-public-api', views.MarketplaceAPIViewSet,
                    basename='marketplace-public-api'),
    router.register(r'marketplace-offering-files', views.OfferingFileViewSet,
                    basename='marketplace-offering-file'),


urlpatterns = [
    url(r'^api/customers/(?P<uuid>[a-f0-9]+)/offerings/$', views.CustomerOfferingViewSet.as_view()),
    url(r'^api/marketplace-plugins/$', views.PluginViewSet.as_view()),
]
