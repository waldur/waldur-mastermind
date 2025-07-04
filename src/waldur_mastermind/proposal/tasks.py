import logging

from celery import shared_task
from django.utils import timezone

from waldur_core.core.log import event_logger
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

        event_logger.info(
            f"Proposal {proposal.name} has been canceled.",
            event_type="proposal_canceled",
            event_context={"proposal": proposal},
            group="proposal",
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

            event_logger.info(
                f"Review for {review.proposal.name} has been canceled.",
                event_type="review_canceled",
                event_context={"review": review},
                group="review",
            )
            logger.info(f"Review {review.proposal.name} has been canceled.")
