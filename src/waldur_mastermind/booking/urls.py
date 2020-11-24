from waldur_mastermind.booking import views


def register_in(router):
    router.register(
        r'booking-resources', views.ResourceViewSet, basename='booking-resource'
    )
    router.register(
        r'marketplace-bookings',
        views.OfferingBookingsViewSet,
        basename='marketplace-bookings',
    )
