from django.urls import re_path

from waldur_mastermind.proposal import views


def register_in(router):
    router.register(
        r"call-managing-organisations",
        views.CallManagingOrganisationViewSet,
        basename="call-managing-organisation",
    )
    router.register(
        r"proposal-public-calls",
        views.PublicCallViewSet,
        basename="proposal-public-call",
    )
    router.register(
        r"proposal-protected-calls",
        views.ProtectedCallViewSet,
        basename="proposal-protected-call",
    )
    router.register(
        r"proposal-proposals",
        views.ProposalViewSet,
        basename="proposal-proposal",
    )
    router.register(
        r"proposal-reviews",
        views.ReviewViewSet,
        basename="proposal-review",
    )
    router.register(
        r"proposal-requested-offerings",
        views.ProviderRequestedOfferingViewSet,
        basename="proposal-requested-offering",
    )
    router.register(
        r"proposal-requested-resources",
        views.ProviderRequestedResourceViewSet,
        basename="proposal-requested-resource",
    )


urlpatterns = [
    re_path(
        r"^api/proposal-protected-calls/(?P<uuid>[a-f0-9]+)/%ss/(?P<obj_uuid>[a-f0-9]+)/$"
        % action,
        views.ProtectedCallViewSet.as_view(
            {
                "get": f"{action}_detail",
                "delete": f"{action}_detail",
                "patch": f"{action}_detail",
                "put": f"{action}_detail",
            }
        ),
        name=f"proposal-call-{action}-detail",
    )
    for action in ["offering", "round"]
]

urlpatterns += [
    re_path(
        r"^api/proposal-proposals/(?P<uuid>[a-f0-9]+)/%ss/(?P<obj_uuid>[a-f0-9]+)/$"
        % action,
        views.ProposalViewSet.as_view(
            {
                "get": f"{action}_detail",
                "delete": f"{action}_detail",
                "patch": f"{action}_detail",
                "put": f"{action}_detail",
            }
        ),
        name=f"proposal-proposal-{action}-detail",
    )
    for action in ["resource"]
]
