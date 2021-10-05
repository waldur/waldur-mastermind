from . import views


def register_in(router):
    router.register(
        r'marketplace-customer-creation-requests',
        views.CustomerCreateRequestViewSet,
        basename='marketplace-customer-creation-request',
    )

    router.register(
        r'marketplace-project-creation-requests',
        views.ProjectCreateRequestViewSet,
        basename='marketplace-project-creation-request',
    )

    router.register(
        r'marketplace-resource-creation-requests',
        views.ResourceCreateRequestViewSet,
        basename='marketplace-resource-creation-request',
    )

    router.register(
        r'marketplace-resource-creation-flows',
        views.FlowViewSet,
        basename='marketplace-resource-creation-flow',
    )

    router.register(
        r'marketplace-offering-activate-requests',
        views.OfferingActivateRequestViewSet,
        basename='marketplace-offering-activate-request',
    )
