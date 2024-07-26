from . import views


def register_in(router):
    router.register(
        r"marketplace-project-estimated-cost-policies",
        views.ProjectEstimatedCostPolicyViewSet,
        basename="marketplace-project-estimated-cost-policy",
    )
    router.register(
        r"marketplace-customer-estimated-cost-policies",
        views.CustomerEstimatedCostPolicyViewSet,
        basename="marketplace-customer-estimated-cost-policy",
    )
    router.register(
        r"marketplace-offering-estimated-cost-policies",
        views.OfferingEstimatedCostPolicyViewSet,
        basename="marketplace-offering-estimated-cost-policy",
    )
