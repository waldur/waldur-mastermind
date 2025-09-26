import logging
from typing import cast

from django.db import transaction
from django.db.models import OuterRef

from waldur_core.core.utils import SubqueryCount, get_system_robot
from waldur_core.permissions.utils import get_users
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal import tasks
from waldur_mastermind.proposal.enums import ProposalStates, RequestedOfferingStates

logger = logging.getLogger(__name__)


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
    number_of_needed_reviewers = max(
        0,
        proposal.round.minimum_number_of_reviewers
        or 0
        - proposal.review_set.exclude(
            state=proposal_models.Review.States.REJECTED
        ).count(),
    )
    return available_reviewer[:number_of_needed_reviewers]


def process_proposals_pending_reviewers(proposal: proposal_models.Proposal):
    for reviewer in get_available_reviewer(proposal):
        proposal_models.Review.objects.create(reviewer=reviewer, proposal=proposal)

    # Only update state and send notification if the state is actually changing
    if proposal.state != ProposalStates.IN_REVIEW:
        old_state = proposal.state
        proposal.state = ProposalStates.IN_REVIEW
        tasks.notify_user_about_proposal_state_update.delay(
            proposal.uuid, old_state, proposal.state
        )
        return proposal.save()
    return proposal


def allocate_proposal(proposal: proposal_models.Proposal):
    proposal_round = proposal.round
    name = proposal.name
    start_date = None
    call_prefix = proposal_round.call.backend_id or proposal_round.call.slug
    project_name = " - ".join(
        [call_prefix, proposal_round.start_time.strftime("%Y-%m-%d"), name]
    )[: structure_models.PROJECT_NAME_LENGTH]

    if (
        proposal.round.allocation_time
        == proposal_models.Round.AllocationTimes.FIXED_DATE
    ):
        start_date = proposal.round.allocation_date

    project = structure_models.Project.objects.create(
        customer=proposal_round.call.manager.customer,
        name=project_name,
        start_date=start_date,
    )
    project = cast(structure_models.Project, project)

    if start_date:
        logger.info(
            f"Field start_date of {project} has been changed to {proposal.round.allocation_date}."
        )

    proposal.project = project
    proposal.save()

    requested_resources = proposal.requestedresource_set.filter(
        requested_offering__state=RequestedOfferingStates.ACCEPTED
    )

    for mapping in proposal.round.call.proposalprojectrolemapping_set.all():  # type: ignore
        users = get_users(proposal, mapping.proposal_role)
        for user in users:
            if mapping.project_role:
                project.add_user(user, mapping.project_role)
            else:
                continue

    for requested_resource in requested_resources:
        with transaction.atomic():
            attrs = dict(
                project=project,
                offering=requested_resource.requested_offering.offering,
                plan=requested_resource.requested_offering.plan,
                attributes=requested_resource.attributes,
                limits=requested_resource.limits,
            )
            resource = marketplace_models.Resource(
                **attrs,
                name=project.name,
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


def create_reviews_of_round(call_round: proposal_models.Round):
    call_round.proposal_set.filter(state=ProposalStates.DRAFT).update(
        state=ProposalStates.CANCELED
    )

    for proposal in call_round.proposal_set.filter(
        state__in=(
            ProposalStates.SUBMITTED,
            ProposalStates.IN_REVIEW,
        )
    ):
        process_proposals_pending_reviewers(proposal)


def get_proposal_review_counts(proposal: proposal_models.Proposal) -> dict:
    base_queryset = proposal_models.Review.objects.filter(proposal=proposal)

    submitted_reviews = base_queryset.filter(
        state=proposal_models.Review.States.SUBMITTED
    ).count()

    rejected_reviews = base_queryset.filter(
        state=proposal_models.Review.States.REJECTED
    ).count()

    pending_reviews = base_queryset.filter(
        state__in=[
            proposal_models.Review.States.CREATED,
            proposal_models.Review.States.IN_REVIEW,
        ]
    ).count()

    return {
        "submitted_reviews": submitted_reviews,
        "rejected_reviews": rejected_reviews,
        "pending_reviews": pending_reviews,
    }
