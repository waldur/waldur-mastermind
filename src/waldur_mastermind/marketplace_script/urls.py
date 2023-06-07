from . import views


def register_in(router):
    router.register(
        r'marketplace-script-dry-run',
        views.DryRunView,
        basename='marketplace-script-dry-run',
    )
    router.register(
        r'marketplace-script-async-dry-run',
        views.AsyncDryRunView,
        basename='marketplace-script-async-dry-run',
    )
