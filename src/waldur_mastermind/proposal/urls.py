from django.urls import re_path

from waldur_mastermind.proposal import views


def register_in(router):
    router.register(
        r"call-managing-organisations",
        views.CallManagingOrganisationViewSet,
        basename="call-managing-organisation",
    )
    router.register(
        r"call-templates",
        views.CallTemplateViewSet,
        basename="call-template",
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
        r"proposal-my-tasks",
        views.MyTaskViewSet,
        basename="proposal-my-task",
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
        r"proposal-offering-demand",
        views.OfferingDemandViewSet,
        basename="proposal-offering-demand",
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
        r"proposal-rankings",
        views.ProposalRankingViewSet,
        basename="proposal-ranking",
    )
    router.register(
        r"round-meeting-notes",
        views.RoundMeetingNoteViewSet,
        basename="round-meeting-note",
    )
    router.register(
        r"call-proposal-project-role-mappings",
        views.ProposalProjectRoleMappingViewSet,
        basename="call-proposal-project-role-mapping",
    )
    router.register(
        r"proposal-role-assignments",
        views.ProposalRoleAssignmentViewSet,
        basename="proposal-role-assignment",
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
    # The operator's reviewer registry (staff only)
    router.register(
        r"reviewer-registry",
        views.ReviewerRegistryViewSet,
        basename="reviewer-registry",
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
    for action in [
        "offering",
        "round",
        "resource_template",
        "offering_component",
        "resource_pool",
        "workflow_step",
        "proposal_role",
        "panel",
        "notification_override",
    ]
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
    for action in ["resource", "awarded_resource"]
]

urlpatterns += [
    re_path(
        r"^api/proposal-proposals/(?P<uuid>[a-f0-9]+)/runs/(?P<obj_uuid>[a-f0-9]+)/$",
        views.ProposalViewSet.as_view({"get": "run_detail", "patch": "run_detail"}),
        name="proposal-proposal-run-detail",
    ),
]

urlpatterns += [
    re_path(
        r"^api/proposal-protected-calls/(?P<uuid>[a-f0-9]+)/rounds/(?P<obj_uuid>[a-f0-9]+)/close/$",
        views.ProtectedCallViewSet.as_view({"post": "close_round"}),
        name="proposal-call-close_round",
    ),
]
urlpatterns += [
    re_path(
        rf"^api/proposal-protected-calls/(?P<uuid>[a-f0-9]+)/rounds/(?P<obj_uuid>[a-f0-9]+)/{action}/$",
        views.ProtectedCallViewSet.as_view({"post": f"{action}_round"}),
        name=f"proposal-call-{action}_round",
    )
    for action in (
        "start_deciding",
        "publish_results",
        "complete",
        "record_adoption",
        "complete_workflow_step",
        "open_workflow_step",
        "advance_workflow_step",
        "set_workflow_step_due_date",
        "allocate_in_submission_order",
    )
]
urlpatterns += [
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
