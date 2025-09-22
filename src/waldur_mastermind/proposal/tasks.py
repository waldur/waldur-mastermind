import logging
from typing import Any, cast

from celery import shared_task
from constance import config
from django.db.models import Avg, Count, Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.permissions import _get_customer
from waldur_mastermind.marketplace import models as marketplace_models
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
    date = timezone.now()
    cancellation_date = date.strftime("%Y-%m-%d %H:%M:%S")
    for proposal in proposal_models.Proposal.objects.exclude(
        state__in=(
            ProposalStates.ACCEPTED,
            ProposalStates.REJECTED,
            ProposalStates.CANCELED,
        )
    ).filter(round__cutoff_time__lt=date):
        proposal.state = ProposalStates.CANCELED
        proposal.save(update_fields=["state"])

        event_logger.emit(
            f"Proposal {proposal.name} has been canceled.",
            event_type=EventType.PROPOSAL_CANCELED,
            event_context={"proposal": proposal},
            scopes=[_get_customer(proposal)],
        )
        logger.info(f"Proposal {proposal.name} has been canceled.")

        # Schedule notification to the proposal creator
        notify_proposal_creator_about_cancelled_proposal.apply_async(
            args=(proposal.uuid, cancellation_date),
            countdown=10,  # 10 second delay
        )


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

    if not proposal.created_by or not proposal.created_by.email:
        logger.warning(
            f"Cannot send proposal state update notification. Proposal {proposal.uuid} creator has no valid email."
        )

    proposal_link = core_utils.format_homeport_link(
        "proposals/{proposal_uuid}/",
        proposal_uuid=proposal.uuid,
    )
    project_link = None
    allocated_resources = None
    if new_state == ProposalStates.ACCEPTED:
        try:
            project_link = core_utils.format_homeport_link(
                "projects/{project_uuid}/",
                project_uuid=proposal.project.uuid,  # type: ignore
            )
            resources = marketplace_models.Resource.objects.filter(
                project=proposal.project
            ).select_related("offering", "plan")

            allocated_resources = [
                {
                    "name": resource.name,
                    "provider_name": resource.offering.customer.name
                    if resource.offering.customer
                    else "N/A",
                    "plan_name": resource.plan.name if resource.plan else "Default",
                }
                for resource in resources
            ]
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
        "proposal_creator_name": proposal.created_by.full_name
        if proposal.created_by
        else "Unknown",
        "call_name": proposal.round.call.name,
        "update_date": proposal.modified,
        "duration": proposal.duration_in_days,
        "rejection_feedback": proposal.allocation_comment,
        "review_period": proposal.round.review_duration_in_days,
        "allocated_resources": allocated_resources
        if new_state == ProposalStates.ACCEPTED
        else None,
    }

    core_utils.broadcast_mail(
        "proposal",
        "proposal_state_changed",
        context,
        [proposal.created_by.email]
        if proposal.created_by and proposal.created_by.email
        else [],
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
        "proposal_creator_name": proposal.created_by.full_name
        if proposal.created_by
        else "Unknown",
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


@shared_task(name="waldur_mastermind.proposal.notify_call_managers_about_new_review")
def notify_call_managers_about_new_review(review_uuid):
    review = proposal_models.Review.objects.get(uuid=review_uuid)
    review_counts = utils.get_proposal_review_counts(review.proposal)

    context = {
        "site_name": config.SITE_NAME,
        "review_url": core_utils.format_homeport_link(
            "call-management/{customer_uuid}/review/{review_uuid}/",
            customer_uuid=review.proposal.round.call.manager.customer.uuid,
            review_uuid=review.uuid,
        ),
        "proposal_name": review.proposal.name,
        "call_name": review.proposal.round.call.name,
        "reviewer_name": review.reviewer.full_name,
        "submission_date": review.modified,
        "score": review.summary_score,
        "max_score": "5",
        **review_counts,
    }

    recipients = list(
        review.proposal.round.call.call_managers.values_list("email", flat=True)
    )
    core_utils.broadcast_mail(
        "proposal",
        "new_review_submitted",
        context,
        recipients,
    )


@shared_task(
    name="waldur_mastermind.proposal.notify_call_managers_about_rejected_review"
)
def notify_call_managers_about_rejected_review(review_uuid):
    review = proposal_models.Review.objects.get(uuid=review_uuid)
    review_counts = utils.get_proposal_review_counts(review.proposal)

    context = {
        "site_name": config.SITE_NAME,
        "proposal_name": review.proposal.name,
        "call_name": review.proposal.round.call.name,
        "reviewer_name": review.reviewer.full_name,
        "assign_date": review.created,
        "rejection_date": review.modified,
        "create_review_link": core_utils.format_homeport_link(
            "call-management/{customer_uuid}/proposals/",
            customer_uuid=review.proposal.round.call.manager.customer.uuid,
        ),
        **review_counts,
    }

    recipients = list(
        review.proposal.round.call.call_managers.values_list("email", flat=True)
    )
    core_utils.broadcast_mail(
        "proposal",
        "review_rejected",
        context,
        recipients,
    )


@shared_task(
    name="waldur_mastermind.proposal.notify_proposal_creator_about_cancelled_proposal"
)
def notify_proposal_creator_about_cancelled_proposal(proposal_uuid, cancellation_date):
    proposal = proposal_models.Proposal.objects.get(uuid=proposal_uuid)

    if not proposal.created_by or not proposal.created_by.email:
        logger.warning(
            f"Cannot send proposal cancellation notification. Proposal {proposal.uuid} creator has no valid email."
        )
        return

    proposal_link = core_utils.format_homeport_link(
        "proposals/{proposal_uuid}/",
        proposal_uuid=proposal.uuid,
    )

    context = {
        "site_name": config.SITE_NAME,
        "proposal_name": proposal.name,
        "call_name": proposal.round.call.name,
        "cancellation_date": cancellation_date,
        "proposal_url": proposal_link,
        "proposal_creator_name": proposal.created_by.full_name
        if proposal.created_by
        else "Unknown",
    }

    core_utils.broadcast_mail(
        "proposal",
        "proposal_cancelled",
        context,
        [proposal.created_by.email]
        if proposal.created_by and proposal.created_by.email
        else [],
    )


@shared_task(name="waldur_mastermind.proposal.notify_reviewer_about_assignment")
def notify_reviewer_about_assignment(review_uuid):
    review = proposal_models.Review.objects.get(uuid=review_uuid)

    if not review.reviewer or not review.reviewer.email:
        logger.warning(
            f"Cannot send review assignment notification. Review {review.uuid} reviewer has no valid email."
        )
        return

    link_to_reviews_list = core_utils.format_homeport_link(
        "reviews/",
    )

    context = {
        "site_name": config.SITE_NAME,
        "reviewer_name": review.reviewer.full_name,
        "call_name": review.proposal.round.call.name,
        "proposal_name": review.proposal.name,
        "proposal_creator_name": review.proposal.created_by.full_name
        if review.proposal.created_by
        else "N/A",
        "submission_date": review.proposal.created,
        "review_deadline": review.review_end_date,
        "link_to_reviews_list": link_to_reviews_list,
    }

    core_utils.broadcast_mail(
        "proposal",
        "review_assigned",
        context,
        [review.reviewer.email],
    )


@shared_task(name="waldur_mastermind.proposal.notify_reviewer_on_proposal_decision")
def notify_reviewer_on_proposal_decision(proposal_uuid):
    proposal = proposal_models.Proposal.objects.get(uuid=proposal_uuid)
    reviews = proposal.review_set.filter(state=proposal_models.Review.States.SUBMITTED)

    proposal_link = core_utils.format_homeport_link(
        "proposals/{proposal_uuid}/",
        proposal_uuid=proposal.uuid,
    )

    base_context = {
        "site_name": config.SITE_NAME,
        "proposal_state": proposal.state,
        "proposal_url": proposal_link,
        "proposal_name": proposal.name,
        "call_name": proposal.round.call.name,
        "decision_date": proposal.modified.strftime("%B %d, %Y"),
        "rejection_reason": getattr(proposal, "allocation_comment", None)
        if proposal.state == ProposalStates.REJECTED
        else None,
    }

    for review in reviews:
        if review.reviewer and review.reviewer.email:
            context = {
                **base_context,
                "reviewer_name": review.reviewer.full_name,
            }

            core_utils.broadcast_mail(
                "proposal",
                "proposal_decision_for_reviewer",
                context,
                [review.reviewer.email],
            )
        else:
            logger.warning(
                f"Cannot send proposal decision notification to reviewer for review {review.uuid}. Reviewer has no valid email."
            )


@shared_task(name="waldur_mastermind.proposal.notify_offering_request_decision")
def notify_offering_request_decision(requested_offering_uuid):
    requested_offering = proposal_models.RequestedOffering.objects.get(
        uuid=requested_offering_uuid
    )
    call = requested_offering.call

    recipients = list(call.call_managers.values_list("email", flat=True))

    if not recipients:
        logger.warning(
            f"Cannot send offering request decision notification. Call {call.uuid} has no managers with valid emails."
        )
        return

    call_url = core_utils.format_homeport_link(
        f"calls/{call.uuid}/",
    )

    context = {
        "site_name": config.SITE_NAME,
        "offering_name": requested_offering.offering.name,
        "call_name": call.name,
        "provider_name": requested_offering.offering.customer.name
        if requested_offering.offering.customer
        else "N/A",
        "decision": requested_offering.state,
        "decision_date": requested_offering.modified.strftime("%B %d, %Y"),
        "call_url": call_url,
    }

    core_utils.broadcast_mail(
        "proposal",
        "requested_offering_decision",
        context,
        recipients,
    )


@shared_task(name="waldur_mastermind.proposal.notify_reviewer_on_round_start")
def notify_reviewer_on_round_start():
    today = timezone.now().date()
    rounds = (
        proposal_models.Round.objects.filter(
            call__state=CallStates.ACTIVE,
            start_time__date=today,
        )
        .select_related("call")
        .distinct()
    )
    if not rounds.exists():
        return

    for round in rounds:
        call_url = core_utils.format_homeport_link(
            f"calls/{round.call.uuid}/",
        )

        base_context = {
            "site_name": config.SITE_NAME,
            "call_name": round.call.name,
            "round_name": round.name,
            "start_date": round.start_time,
            "end_date": round.cutoff_time,
            "call_url": call_url,
        }

        reviewers = round.call.reviewers

        if not reviewers.exists():
            # No reviewers - continue
            continue

        for reviewer in reviewers:
            if reviewer.email:
                context = {
                    **base_context,
                    "reviewer_name": reviewer.full_name,
                }

                core_utils.broadcast_mail(
                    "proposal",
                    "round_opening_for_reviewers",
                    context,
                    [reviewer.email],
                )
            else:
                logger.warning(
                    f"Cannot send round creation notification to reviewer {reviewer.uuid}. Reviewer has no valid email."
                )


@shared_task(name="waldur_mastermind.proposal.notify_manager_on_round_cutoff")
def notify_manager_on_round_cutoff():
    now = timezone.now()
    rounds = (
        proposal_models.Round.objects.filter(
            call__state=CallStates.ACTIVE,
            cutoff_time__date=now.date(),
            cutoff_time__hour=now.hour,
        )
        .select_related("call", "call__manager", "call__manager__customer")
        .annotate(
            total_proposals=Count("proposal"),
            total_reviews=Count(
                "proposal__review",
                filter=~Q(
                    proposal__review__state=proposal_models.Review.States.REJECTED
                ),
            ),
        )
        .distinct()
    )
    if not rounds.exists():
        return

    for round_obj in rounds:
        manager_emails = list(
            round_obj.call.call_managers.values_list("email", flat=True)
        )

        if not manager_emails:
            logger.warning(
                f"Cannot send round cutoff notification. Call {round_obj.call.uuid} has no managers with valid emails."
            )
            continue

        round_url = core_utils.format_homeport_link(
            f"call/{round_obj.call.uuid}/round/{round_obj.uuid}/",
        )

        r_any = cast(Any, round_obj)  # pyright typing workaround

        context = {
            "site_name": config.SITE_NAME,
            "call_name": round_obj.call.name,
            "round_name": round_obj.name,
            "total_proposals": r_any.total_proposals,
            "total_reviews": r_any.total_reviews,
            "review_strategy": r_any.get_review_strategy_display(),
            "start_date": round_obj.start_time,
            "close_date": round_obj.cutoff_time,
            "round_url": round_url,
        }

        core_utils.broadcast_mail(
            "proposal",
            "round_closing_for_managers",
            context,
            manager_emails,
        )


@shared_task(
    name="waldur_mastermind.proposal.notify_manager_when_reviews_are_completed"
)
def notify_manager_when_reviews_are_completed(proposal_uuid):
    proposal = proposal_models.Proposal.objects.get(uuid=proposal_uuid)
    completed_reviews = proposal.review_set.filter(
        state=proposal_models.Review.States.SUBMITTED
    )
    incomplete_reviews = proposal.review_set.filter(
        state__in=(
            proposal_models.Review.States.CREATED,
            proposal_models.Review.States.IN_REVIEW,
        )
    )

    if incomplete_reviews.exists() or completed_reviews.count() < (
        proposal.round.minimum_number_of_reviewers or 0
    ):
        return

    call = proposal.round.call
    manager_emails = list(call.call_managers.values_list("email", flat=True))

    if not manager_emails:
        logger.warning(
            f"Cannot send review completion notification. Call {call.uuid} has no managers with valid emails."
        )
        return

    proposal_url = core_utils.format_homeport_link(
        f"call-management/{call.manager.customer.uuid}/proposals/{proposal.uuid}/",
    )

    context = {
        "site_name": config.SITE_NAME,
        "proposal_name": proposal.name,
        "submitter_name": proposal.created_by.full_name
        if proposal.created_by
        else "N/A",
        "call_name": call.name,
        "reviews_count": completed_reviews.count(),
        "average_score": completed_reviews.aggregate(avg_score=Avg("summary_score"))[
            "avg_score"
        ],
        "reviews": [
            {
                "reviewer_name": review.reviewer.full_name
                if review.reviewer
                else "N/A",
                "score": review.summary_score,
                "submitted_at": review.modified,
            }
            for review in completed_reviews
        ],
        "proposal_url": proposal_url,
    }

    core_utils.broadcast_mail(
        "proposal",
        "reviews_complete",
        context,
        manager_emails,
    )
