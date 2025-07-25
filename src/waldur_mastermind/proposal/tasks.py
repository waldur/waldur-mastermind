import logging

from celery import shared_task
from constance import config
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.permissions import _get_customer
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal import utils
from waldur_mastermind.proposal.enums import CallStates, ProposalStates

logger = logging.getLogger(__name__)


@shared_task(
    name="waldur_mastermind.proposal.create_reviews_if_strategy_is_after_round"
)
def create_reviews_if_strategy_is_after_round():
    """Create reviews for active rounds with 'after round' review strategy."""
    rounds = proposal_models.Round.objects.filter(
        start_time__lte=timezone.now(),
        cutoff_time__gte=timezone.now(),
        call__state=CallStates.ACTIVE,
        review_strategy=proposal_models.Round.ReviewStrategies.AFTER_ROUND,
    )

    for r in rounds:
        utils.create_reviews_of_round(r)


@shared_task(
    name="waldur_mastermind.proposal.create_reviews_if_strategy_is_after_proposal"
)
def create_reviews_if_strategy_is_after_proposal():
    """Create reviews for active rounds with 'after proposal' review strategy."""
    rounds = proposal_models.Round.objects.filter(
        call__state=CallStates.ACTIVE,
        review_strategy=proposal_models.Round.ReviewStrategies.AFTER_PROPOSAL,
    )

    for r in rounds:
        for proposal in r.proposal_set.filter(
            state__in=(
                ProposalStates.SUBMITTED,
                ProposalStates.IN_REVIEW,
            )
        ):
            utils.process_proposals_pending_reviewers(proposal)


@shared_task(
    name="waldur_mastermind.proposal.proposals_for_ended_rounds_should_be_cancelled"
)
def proposals_for_ended_rounds_should_be_cancelled():
    """Cancel proposals for rounds that have ended."""
    for proposal in proposal_models.Proposal.objects.exclude(
        state__in=(
            ProposalStates.ACCEPTED,
            ProposalStates.REJECTED,
            ProposalStates.CANCELED,
        )
    ).filter(round__cutoff_time__lt=timezone.now()):
        proposal.state = ProposalStates.CANCELED
        proposal.save(update_fields=["state"])

        event_logger.emit(
            f"Proposal {proposal.name} has been canceled.",
            event_type=EventType.PROPOSAL_CANCELED,
            event_context={"proposal": proposal},
            scopes=[_get_customer(proposal)],
        )
        logger.info(f"Proposal {proposal.name} has been canceled.")


@shared_task(name="waldur_mastermind.proposal.expired_reviews_should_be_cancelled")
def expired_reviews_should_be_cancelled():
    """Cancel reviews that have expired."""
    for review in proposal_models.Review.objects.filter(
        state__in=(
            proposal_models.Review.States.IN_REVIEW,
            proposal_models.Review.States.CREATED,
        )
    ):
        if review.review_end_date <= timezone.now():
            review.state = proposal_models.Review.States.REJECTED
            review.save(update_fields=["state"])

            event_logger.emit(
                f"Review for {review.proposal.name} has been canceled.",
                event_type=EventType.REVIEW_CANCELED,
                event_context={"review": review},
                scopes=[_get_customer(review)],
            )
            logger.info(f"Review {review.proposal.name} has been canceled.")


@shared_task(name="waldur_mastermind.proposal.notify_user_about_proposal_state_update")
def notify_user_about_proposal_state_update(proposal_uuid, previous_state, new_state):
    proposal = proposal_models.Proposal.objects.get(uuid=proposal_uuid)

    if not proposal.created_by.email:  # type: ignore
        logger.warning(
            f"Cannot send proposal state update notification. Proposal {proposal.uuid} creator has no valid email."
        )

    proposal_link = core_utils.format_homeport_link(
        "proposals/{proposal_uuid}/",
        proposal_uuid=proposal.uuid,
    )
    project_link = None
    if new_state == ProposalStates.ACCEPTED:
        try:
            project_link = core_utils.format_homeport_link(
                "projects/{project_uuid}/",
                project_uuid=proposal.project.uuid,  # type: ignore
            )
        except AttributeError:
            pass

    context = {
        "site_name": config.SITE_NAME,
        "new_state": new_state,
        "previous_state": previous_state,
        "proposal_url": proposal_link,
        "project_url": project_link,
        "project_name": proposal.project.name if proposal.project else None,
        "proposal_name": proposal.name,
        "proposal_creator_name": proposal.created_by.full_name,  # type: ignore
        "call_name": proposal.round.call.name,
        "update_date": proposal.modified,
        "duration": proposal.duration_in_days,
        "rejection_feedback": proposal.allocation_comment,
        "review_period": proposal.round.review_duration_in_days,
    }

    core_utils.broadcast_mail(
        "proposal",
        "proposal_state_changed",
        context,
        [proposal.created_by.email],  # type: ignore
    )


@shared_task(
    name="waldur_mastermind.proposal.notify_call_managers_about_new_proposal_submission"
)
def notify_call_managers_about_new_proposal_submission(proposal_uuid):
    proposal = proposal_models.Proposal.objects.get(uuid=proposal_uuid)

    context = {
        "site_name": config.SITE_NAME,
        "proposal_url": core_utils.format_homeport_link(
            "call-management/{customer_uuid}/proposals/{proposal_uuid}/",
            customer_uuid=proposal.round.call.manager.customer.uuid,
            proposal_uuid=proposal.uuid,
        ),
        "proposal_name": proposal.name,
        "proposal_creator_name": proposal.created_by.full_name,  # type: ignore
        "call_name": proposal.round.call.name,
        "round_name": proposal.round.name,
        "submission_date": proposal.modified,
    }

    recipients = list(proposal.round.call.call_managers.values_list("email", flat=True))

    core_utils.broadcast_mail(
        "proposal",
        "new_proposal_submitted",
        context,
        recipients,
    )
