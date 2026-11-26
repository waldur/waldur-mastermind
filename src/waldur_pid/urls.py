from waldur_pid import views


def register_in(router):
    router.register(
        r"marketplace-offering-referrals",
        views.OfferingReferralsViewSet,
        basename="marketplace-offering-referral",
    )
