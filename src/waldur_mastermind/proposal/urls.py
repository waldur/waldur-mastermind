from waldur_mastermind.proposal import views


def register_in(router):
    router.register(
        r'proposal-call-managers',
        views.CallManagerViewSet,
        basename='proposal-call-manager',
    )
