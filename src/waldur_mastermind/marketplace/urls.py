from django.urls import re_path

from waldur_mastermind.marketplace import views


def register_in(router):
    router.register(
        r'marketplace-service-providers',
        views.ServiceProviderViewSet,
        basename='marketplace-service-provider',
    )
    router.register(
        r'marketplace-categories',
        views.CategoryViewSet,
        basename='marketplace-category',
    )
    router.register(
        r'marketplace-provider-offerings',
        views.ProviderOfferingViewSet,
        basename='marketplace-provider-offering',
    )
    router.register(
        r'marketplace-offering-permissions',
        views.OfferingPermissionViewSet,
        basename='marketplace-offering-permission',
    )
    router.register(
        r'marketplace-offering-permissions-log',
        views.OfferingPermissionLogViewSet,
        basename='marketplace-offering-permission_log',
    )
    router.register(
        r'marketplace-plans', views.ProviderPlanViewSet, basename='marketplace-plan'
    )
    router.register(
        r'marketplace-plan-components',
        views.PlanComponentViewSet,
        basename='marketplace-plan-component',
    )
    router.register(
        r'marketplace-public-plans',
        views.PublicPlanViewSet,
        basename='marketplace-public-plan',
    )
    router.register(
        r'marketplace-screenshots',
        views.ScreenshotViewSet,
        basename='marketplace-screenshot',
    )
    router.register(
        r'marketplace-cart-items',
        views.CartItemViewSet,
        basename='marketplace-cart-item',
    )
    router.register(
        r'marketplace-orders', views.OrderViewSet, basename='marketplace-order'
    )
    router.register(
        r'marketplace-order-items',
        views.OrderItemViewSet,
        basename='marketplace-order-item',
    )
    router.register(
        r'marketplace-resources', views.ResourceViewSet, basename='marketplace-resource'
    )
    router.register(
        r'marketplace-category-component-usages',
        views.CategoryComponentUsageViewSet,
        basename='marketplace-category-component-usage',
    )
    router.register(
        r'marketplace-component-usages',
        views.ComponentUsageViewSet,
        basename='marketplace-component-usage',
    )
    router.register(
        r'marketplace-public-api',
        views.MarketplaceAPIViewSet,
        basename='marketplace-public-api',
    )
    router.register(
        r'marketplace-offering-files',
        views.OfferingFileViewSet,
        basename='marketplace-offering-file',
    )
    router.register(
        r'marketplace-offering-referrals',
        views.OfferingReferralsViewSet,
        basename='marketplace-offering-referral',
    )
    router.register(
        r'marketplace-offering-users',
        views.OfferingUsersViewSet,
        basename='marketplace-offering-user',
    )
    router.register(
        r'marketplace-stats',
        views.StatsViewSet,
        basename='marketplace-stats',
    )
    router.register(
        r'provider-invoice-items',
        views.ProviderInvoiceItemsViewSet,
        basename='provider-invoice-items',
    )
    router.register(
        r'marketplace-public-offerings',
        views.PublicOfferingViewSet,
        basename='marketplace-public-offering',
    )
    router.register(
        r'marketplace-robot-accounts',
        views.RobotAccountViewSet,
        basename='marketplace-robot-account',
    )


urlpatterns = [
    re_path(r'^api/marketplace-plugins/$', views.PluginViewSet.as_view()),
    re_path(
        r'^api/marketplace-resource-offerings/(?P<project_uuid>[a-f0-9]+)/(?P<category_uuid>[a-f0-9]+)/$',
        views.ResourceOfferingsViewSet.as_view(),
    ),
    re_path(
        r'^api/marketplace-runtime-states/(?P<project_uuid>[a-f0-9]+)/$',
        views.RuntimeStatesViewSet.as_view(),
    ),
    re_path(
        r'^api/marketplace-related-customers/(?P<customer_uuid>[a-f0-9]+)/$',
        views.RelatedCustomersViewSet.as_view(),
    ),
]
