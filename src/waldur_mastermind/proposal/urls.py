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
    router.register(
        r"proposal-my-requested-resources",
        views.UserRequestedResourceViewSet,
        basename="proposal-my-requested-resource",
    )
    router.register(
        r"call-rounds",
        views.RoundViewSet,
        basename="call-round",
    )
    router.register(
        r"call-proposal-project-role-mappings",
        views.ProposalProjectRoleMappingViewSet,
        basename="call-proposal-project-role-mapping",
    )
    router.register(
        r"call-workflow-step-notification-rules",
        views.CallWorkflowStepNotificationRuleViewSet,
        basename="call-workflow-step-notification-rule",
    )
    # Reviewer Profile endpoints
    router.register(
        r"reviewer-profiles",
        views.ReviewerProfileViewSet,
        basename="reviewer-profile",
    )
    router.register(
        r"expertise-categories",
        views.ExpertiseCategoryViewSet,
        basename="expertise-category",
    )
    # COI endpoints
    router.register(
        r"conflicts-of-interest",
        views.ConflictOfInterestViewSet,
        basename="conflict-of-interest",
    )
    router.register(
        r"coi-disclosures",
        views.COIDisclosureViewSet,
        basename="coi-disclosure",
    )
    router.register(
        r"call-reviewer-pools",
        views.CallReviewerPoolViewSet,
        basename="call-reviewer-pool",
    )
    router.register(
        r"coi-detection-jobs",
        views.COIDetectionJobViewSet,
        basename="coi-detection-job",
    )
    # Reviewer bids
    router.register(
        r"reviewer-bids",
        views.ReviewerBidViewSet,
        basename="reviewer-bid",
    )
    # Reviewer suggestions
    router.register(
        r"reviewer-suggestions",
        views.ReviewerSuggestionViewSet,
        basename="reviewer-suggestion",
    )
    # Assignment batches and items
    router.register(
        r"assignment-batches",
        views.AssignmentBatchViewSet,
        basename="assignment-batch",
    )
    router.register(
        r"assignment-items",
        views.AssignmentItemViewSet,
        basename="assignment-item",
    )
    router.register(
        r"call-assignment-configurations",
        views.CallAssignmentConfigurationViewSet,
        basename="call-assignment-configuration",
    )
    router.register(
        r"my-assignment-batches",
        views.MyAssignmentBatchViewSet,
        basename="my-assignment-batch",
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
    for action in ["offering", "round", "resource_template", "workflow_step"]
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

urlpatterns += [
    re_path(
        r"^api/proposal-protected-calls/(?P<uuid>[a-f0-9]+)/rounds/(?P<obj_uuid>[a-f0-9]+)/close/$",
        views.ProtectedCallViewSet.as_view({"post": "close_round"}),
        name="proposal-call-close_round",
    ),
    re_path(
        r"^api/proposal-proposals/(?P<uuid>[a-f0-9]+)/resources/(?P<obj_uuid>[a-f0-9]+)/purchase_order/$",
        views.ProposalViewSet.as_view(
            {
                "post": "resource_purchase_order",
                "delete": "resource_purchase_order",
            }
        ),
        name="proposal-proposal-resource-purchase-order",
    ),
]

# Public reviewer invitation endpoints (token-based, no auth required)
urlpatterns += [
    re_path(
        r"^api/reviewer-invitations/(?P<token>[a-zA-Z0-9_-]+)/$",
        views.PublicReviewerInvitationViewSet.as_view({"get": "retrieve"}),
        name="reviewer-invitation-detail",
    ),
    re_path(
        r"^api/reviewer-invitations/(?P<token>[a-zA-Z0-9_-]+)/accept/$",
        views.PublicReviewerInvitationViewSet.as_view({"post": "accept"}),
        name="reviewer-invitation-accept",
    ),
    re_path(
        r"^api/reviewer-invitations/(?P<token>[a-zA-Z0-9_-]+)/decline/$",
        views.PublicReviewerInvitationViewSet.as_view({"post": "decline"}),
        name="reviewer-invitation-decline",
    ),
]
