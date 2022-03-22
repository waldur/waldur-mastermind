from . import views


def register_in(router):
    router.register(
        r'marketplace-script-dry-run',
        views.DryRunView,
        basename='marketplace-script-dry-run',
    )
