from . import views


def register_in(router):
    router.register(
        r"marketplace-project-estimated-cost-policies",
        views.ProjectEstimatedCostPolicyViewSet,
        basename="marketplace-project-estimated-cost-policy",
    )
