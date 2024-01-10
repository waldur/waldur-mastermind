from django.urls import re_path

from . import views


def register_in(router):
    router.register(
        r"marketplace-script-dry-run",
        views.DryRunView,
        basename="marketplace-script-dry-run",
    )
    router.register(
        r"marketplace-script-async-dry-run",
        views.AsyncDryRunView,
        basename="marketplace-script-async-dry-run",
    )


urlpatterns = [
    re_path(
        r"^api/marketplace-script-sync-resource/$",
        views.PullMarketplaceScriptResourceView.as_view(),
        name="marketplace-script-sync-resource",
    ),
]
