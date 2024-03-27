from django.db import transaction
from django.db.models import OuterRef, QuerySet

from waldur_core.core.utils import SubqueryCount, get_system_robot
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models as proposal_models


def get_available_reviewer(proposal: proposal_models.Proposal):
    reviewer_ids = proposal.review_set.values_list("reviewer_id", flat=True)
    reviews = proposal_models.Review.objects.filter(
        reviewer_id=OuterRef("pk"), proposal__round__call=proposal.round.call
    ).exclude(state=proposal_models.Review.States.REJECTED)
    available_reviewer = (
        proposal.round.call.reviewers.exclude(id__in=reviewer_ids)
        .annotate(reviewers_count=SubqueryCount(reviews))
        .order_by("reviewers_count")
    )
    number_of_needed_reviewers = (
        proposal.round.minimum_number_of_reviewers
        - proposal.review_set.exclude(
            state=proposal_models.Review.States.REJECTED
        ).count()
    )
    return available_reviewer[:number_of_needed_reviewers]


def process_proposals_pending_reviewers(proposal: proposal_models.Proposal):
    for reviewer in get_available_reviewer(proposal):
        proposal_models.Review.objects.create(reviewer=reviewer, proposal=proposal)

    proposal.state = proposal_models.Proposal.States.IN_REVIEW
    return proposal.save()


def allocate_proposal(proposal: proposal_models.Proposal):
    requested_resources: QuerySet[
        proposal_models.RequestedResource
    ] = proposal.requestedresource_set.filter(
        requested_offering__state=proposal_models.RequestedOffering.States.ACCEPTED
    )

    project_role = proposal.round.call.default_project_role or RoleEnum.PROJECT_ADMIN
    for user in get_users(proposal):
        proposal.project.add_user(user, project_role)

    for requested_resource in requested_resources:
        with transaction.atomic():
            attrs = dict(
                project=proposal.project,
                offering=requested_resource.requested_offering.offering,
                plan=requested_resource.requested_offering.plan,
                attributes=requested_resource.attributes,
                limits=requested_resource.limits,
            )
            resource = marketplace_models.Resource(
                **attrs,
                name=proposal.project.name,
            )
            resource.init_cost()
            resource.save()

            order = marketplace_models.Order(
                **attrs,
                resource=resource,
                created_by=get_system_robot(),
            )
            order.init_cost()
            order.save()

            requested_resource.resource = resource
            requested_resource.save()
