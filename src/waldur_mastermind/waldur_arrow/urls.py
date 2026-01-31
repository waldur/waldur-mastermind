from django.urls import include, re_path

from waldur_core.core.routers import SortedDefaultRouter

from . import views

# Create a dedicated router for Arrow admin endpoints
router = SortedDefaultRouter()

router.register(
    r"settings",
    views.ArrowSettingsViewSet,
    basename="admin-arrow-settings",
)
router.register(
    r"customer-mappings",
    views.ArrowCustomerMappingViewSet,
    basename="admin-arrow-customer-mapping",
)
router.register(
    r"vendor-offering-mappings",
    views.ArrowVendorOfferingMappingViewSet,
    basename="admin-arrow-vendor-offering-mapping",
)
router.register(
    r"billing-syncs",
    views.ArrowBillingSyncViewSet,
    basename="admin-arrow-billing-sync",
)
router.register(
    r"consumption-records",
    views.ArrowConsumptionRecordViewSet,
    basename="admin-arrow-consumption-record",
)
router.register(
    r"billing-sync-items",
    views.ArrowBillingSyncItemViewSet,
    basename="admin-arrow-billing-sync-item",
)


def register_in(router):
    """No registration in the main router - Arrow uses its own nested router."""
    pass


# URL patterns under /api/admin/arrow/
urlpatterns = [
    re_path(r"^api/admin/arrow/", include(router.urls)),
]
