from . import views


def register_in(router):
    router.register(
        r"openstack-marketplace-tenants",
        views.MarketplaceTenantActionsViewSet,
        basename="openstack-marketplace-tenant",
    )
    router.register(
        r"marketplace-openstack-duplicate-offerings",
        views.DuplicateTenantOfferingViewSet,
        basename="marketplace-openstack-duplicate-offering",
    )


urlpatterns = []
