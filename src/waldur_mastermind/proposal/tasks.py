import logging
from datetime import timedelta
from typing import Any, cast

from celery import shared_task
from constance import config
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.permissions import _get_customer
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal import utils, workflow_service
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    WorkflowStepInstanceStatuses,
)

logger = logging.getLogger(__name__)


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
        state=proposal_models.Review.States.IN_REVIEW
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


@shared_task(
    name="waldur_mastermind.proposal.notify_reviewer_about_assignment",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,  # Max 10 minutes between retries
    max_retries=3,
)
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


@shared_task(
    name="waldur_mastermind.proposal.notify_reviewer_on_review_deadline_approaching"
)
def notify_reviewer_on_review_deadline_approaching():
    now = timezone.now()
    reviews = proposal_models.Review.objects.filter(
        state=proposal_models.Review.States.IN_REVIEW,
        proposal__round__call__state=CallStates.ACTIVE,
    ).select_related("reviewer", "proposal", "proposal__round", "proposal__round__call")

    for review in reviews:
        review_deadline = review.review_end_date
        if not review_deadline:
            continue

        if review_deadline <= now:
            continue

        time_remaining_days = (review_deadline.date() - now.date()).days
        if time_remaining_days < 0 or time_remaining_days > 3:
            continue

        if not review.reviewer or not review.reviewer.email:
            logger.warning(
                f"Cannot send review deadline reminder. Review {review.uuid} reviewer has no valid email."
            )
            continue

        context = {
            "site_name": config.SITE_NAME,
            "reviewer_name": review.reviewer.full_name,
            "proposal_name": review.proposal.name,
            "call_name": review.proposal.round.call.name,
            "review_deadline": review_deadline,
            "time_remaining_days": time_remaining_days,
            "review_url": core_utils.format_homeport_link("reviews/"),
        }

        core_utils.broadcast_mail(
            "proposal",
            "review_deadline_approaching",
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


def notify_proposal_decision(proposal_uuid, previous_state, new_state):
    """Enqueue the applicant + reviewer notifications for a proposal decision.

    Every code path that accepts or rejects a proposal — the legacy
    approve/reject actions, the workflow engine's terminal step, and any future
    automatic allocator — must call this so the decision emails stay consistent
    and cannot be silently dropped by a new caller. Call it after the state
    change has been committed (or is about to be), never before allocation, so
    the accepted email can include the provisioned project and resources.
    """
    notify_user_about_proposal_state_update.delay(
        proposal_uuid, previous_state, new_state
    )
    notify_reviewer_on_proposal_decision.delay(proposal_uuid)


@shared_task(
    name="waldur_mastermind.proposal.run_coi_detection",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def run_coi_detection(self, job_uuid: str):
    """
    Run automated COI detection for a call in the background.

    This task processes all reviewer-proposal pairs and detects conflicts
    based on co-authorship, institutional affiliations, and named personnel.
    """
    from waldur_mastermind.proposal.coi_detection import run_coi_detection_for_call
    from waldur_mastermind.proposal.enums import COIDetectionJobStates

    try:
        job = proposal_models.COIDetectionJob.objects.get(uuid=job_uuid)
    except proposal_models.COIDetectionJob.DoesNotExist:
        logger.error(f"COI detection job {job_uuid} not found")
        return

    if job.state not in (COIDetectionJobStates.PENDING, COIDetectionJobStates.RUNNING):
        logger.info(f"COI detection job {job_uuid} is in state {job.state}, skipping")
        return

    try:
        # Store celery task ID for tracking
        job.celery_task_id = self.request.id
        job.save(update_fields=["celery_task_id"])

        result = run_coi_detection_for_call(job.call, job)

        logger.info(
            f"COI detection completed for call {job.call.uuid}: "
            f"processed {result['processed']} pairs, found {result['conflicts_found']} conflicts"
        )
        return result

    except Exception as exc:
        job.state = COIDetectionJobStates.FAILED
        job.error_message = str(exc)
        job.save(update_fields=["state", "error_message"])
        logger.exception(f"COI detection failed for job {job_uuid}")

        # Retry on transient errors
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        raise


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
    name="waldur_mastermind.proposal.notify_proposal_creator_on_submission_deadline_approaching"
)
def notify_proposal_creator_on_submission_deadline_approaching():
    now = timezone.now()
    proposals = proposal_models.Proposal.objects.filter(
        state=ProposalStates.DRAFT,
        round__call__state=CallStates.ACTIVE,
        round__cutoff_time__gt=now,
    ).select_related("round", "round__call", "created_by")

    for proposal in proposals:
        time_remaining = proposal.round.cutoff_time - now
        if time_remaining.total_seconds() <= 0:
            continue

        if time_remaining > timedelta(days=3):
            continue

        total_seconds = int(time_remaining.total_seconds())
        remaining_days, remainder = divmod(total_seconds, 24 * 60 * 60)

        if not proposal.created_by or not proposal.created_by.email:
            logger.warning(
                f"Cannot send submission deadline reminder. Proposal {proposal.uuid} creator has no valid email."
            )
            continue

        remaining_hours = remainder // (60 * 60)

        proposal_url = core_utils.format_homeport_link(
            "proposals/{proposal_uuid}/",
            proposal_uuid=proposal.uuid,
        )

        context = {
            "site_name": config.SITE_NAME,
            "proposal_creator_name": proposal.created_by.full_name,
            "proposal_name": proposal.name,
            "call_name": proposal.round.call.name,
            "round_name": proposal.round.name,
            "deadline_date": proposal.round.cutoff_time,
            "time_remaining_days": remaining_days,
            "time_remaining_hours": remaining_hours,
            "proposal_url": proposal_url,
        }

        core_utils.broadcast_mail(
            "proposal",
            "proposal_submission_deadline_approaching",
            context,
            [proposal.created_by.email],
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
        state=proposal_models.Review.States.IN_REVIEW
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


@shared_task(name="waldur_mastermind.proposal.mark_expired_workflow_steps")
def mark_expired_workflow_steps():
    """Expire ACTIVE workflow step instances past their deadline and advance the workflow.

    For each overdue active step, marks it EXPIRED and either activates the
    next enabled step or rejects the proposal when no further step exists.
    Each transition runs in its own transaction so a single failure does not
    block other expiries.
    """
    overdue_ids = list(
        proposal_models.ProposalWorkflowStepInstance.objects.filter(
            status=WorkflowStepInstanceStatuses.ACTIVE,
            deadline__lt=timezone.now(),
        ).values_list("id", flat=True)
    )

    expired_count = 0
    for instance_id in overdue_ids:
        try:
            with transaction.atomic():
                instance = (
                    proposal_models.ProposalWorkflowStepInstance.objects.select_for_update()
                    .filter(
                        id=instance_id,
                        status=WorkflowStepInstanceStatuses.ACTIVE,
                        deadline__lt=timezone.now(),
                    )
                    .first()
                )
                if instance is None:
                    continue
                workflow_service.expire_step(instance)
        except Exception:
            logger.exception(
                "Failed to expire workflow step instance id=%s", instance_id
            )
            continue

        expired_count += 1

    if expired_count:
        logger.info("Expired %d workflow step instance(s)", expired_count)
    return expired_count


@shared_task(name="waldur_mastermind.proposal.mark_expired_assignment_batches")
def mark_expired_assignment_batches():
    """Mark assignment batches as EXPIRED when their deadline passes."""

    from waldur_mastermind.proposal.enums import (
        AssignmentBatchStatuses,
        AssignmentItemStatuses,
    )

    expired_batches = proposal_models.AssignmentBatch.objects.filter(
        status=AssignmentBatchStatuses.SENT,
        expires_at__lt=timezone.now(),
    )

    count = expired_batches.count()
    if count == 0:
        return

    # Get batch UUIDs before update for logging
    batch_uuids = list(expired_batches.values_list("uuid", flat=True))

    # Update batch status
    expired_batches.update(status=AssignmentBatchStatuses.EXPIRED)

    # Also mark pending items as expired
    proposal_models.AssignmentItem.objects.filter(
        batch__uuid__in=batch_uuids,
        status=AssignmentItemStatuses.PENDING,
    ).update(status=AssignmentItemStatuses.EXPIRED)

    logger.info(f"Marked {count} assignment batches as expired: {batch_uuids}")


@shared_task(
    name="waldur_mastermind.proposal.send_assignment_expiry_reminders",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
)
def send_assignment_expiry_reminders():
    """Send reminder to reviewers before their assignment expires."""
    from datetime import timedelta

    from waldur_mastermind.proposal.enums import AssignmentBatchStatuses

    # Get batches that are sent and haven't had reminder sent
    batches = (
        proposal_models.AssignmentBatch.objects.filter(
            status=AssignmentBatchStatuses.SENT,
            reminder_sent=False,
            expires_at__isnull=False,
        )
        .select_related(
            "call",
            "reviewer_pool_entry",
            "reviewer_pool_entry__reviewer",
            "reviewer_pool_entry__reviewer__user",
            "reviewer_pool_entry__invited_user",
        )
        .prefetch_related("call__assignment_configuration")
    )

    count = 0
    for batch in batches:
        # Skip if no expires_at date
        if not batch.expires_at:
            continue

        # Get reminder days from call config
        reminder_days = 2  # Default
        if hasattr(batch.call, "assignment_configuration"):
            try:
                assignment_config = batch.call.assignment_configuration  # type: ignore[attr-defined]
                reminder_days = assignment_config.send_reminder_before_expiry_days
            except proposal_models.CallAssignmentConfiguration.DoesNotExist:
                pass

        # Check if we're within the reminder window
        reminder_threshold = batch.expires_at - timedelta(days=reminder_days)
        if timezone.now() >= reminder_threshold:
            # Get reviewer email
            reviewer_email = None
            reviewer_name = None

            if batch.reviewer_pool_entry.reviewer:
                reviewer_email = batch.reviewer_pool_entry.reviewer.user.email
                reviewer_name = batch.reviewer_pool_entry.reviewer.user.full_name
            elif batch.reviewer_pool_entry.invited_user:
                reviewer_email = batch.reviewer_pool_entry.invited_user.email
                reviewer_name = batch.reviewer_pool_entry.invited_user.full_name
            else:
                reviewer_email = batch.reviewer_pool_entry.invited_email
                reviewer_name = reviewer_email

            if reviewer_email:
                # Mark reminder_sent BEFORE sending to prevent duplicate emails
                # on task retry (race condition fix)
                batch.reminder_sent = True
                batch.save(update_fields=["reminder_sent"])

                # Send reminder notification
                context = {
                    "site_name": config.SITE_NAME,
                    "reviewer_name": reviewer_name,
                    "call_name": batch.call.name,
                    "expires_at": batch.expires_at,
                    "items_count": batch.assignment_items.count(),  # type: ignore[attr-defined]
                    "link": core_utils.format_homeport_link("my-assignments/"),
                }

                core_utils.broadcast_mail(
                    "proposal",
                    "assignment_expiry_reminder",
                    context,
                    [reviewer_email],
                )

                count += 1
            else:
                logger.warning(
                    f"Cannot send expiry reminder for batch {batch.uuid}: no reviewer email"
                )

    if count > 0:
        logger.info(f"Sent {count} assignment expiry reminders")


@shared_task(
    name="waldur_mastermind.proposal.notify_managers_of_expired_batches",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
)
def notify_managers_of_expired_batches():
    """Notify call managers when batches expire without response."""
    from waldur_mastermind.proposal.enums import AssignmentBatchStatuses

    # Get recently expired batches that haven't notified managers
    batches = proposal_models.AssignmentBatch.objects.filter(
        status=AssignmentBatchStatuses.EXPIRED,
        manager_notified=False,
    ).select_related(
        "call",
        "call__manager",
        "call__manager__customer",
        "reviewer_pool_entry",
        "reviewer_pool_entry__reviewer",
        "reviewer_pool_entry__reviewer__user",
        "reviewer_pool_entry__invited_user",
    )

    count = 0
    for batch in batches:
        manager_emails = list(batch.call.call_managers.values_list("email", flat=True))

        if not manager_emails:
            logger.warning(
                f"Cannot send expired batch notification for batch {batch.uuid}: "
                f"call {batch.call.uuid} has no managers with valid emails"
            )
            batch.manager_notified = True
            batch.save(update_fields=["manager_notified"])
            continue

        # Get reviewer name
        reviewer_name = None
        if batch.reviewer_pool_entry.reviewer:
            reviewer_name = batch.reviewer_pool_entry.reviewer.user.full_name
        elif batch.reviewer_pool_entry.invited_user:
            reviewer_name = batch.reviewer_pool_entry.invited_user.full_name
        else:
            reviewer_name = batch.reviewer_pool_entry.invited_email

        context = {
            "site_name": config.SITE_NAME,
            "call_name": batch.call.name,
            "reviewer_name": reviewer_name,
            "items_count": batch.assignment_items.count(),  # type: ignore[attr-defined]
            "sent_at": batch.sent_at,
            "expired_at": batch.expires_at,
            "assignments_url": core_utils.format_homeport_link(
                f"call/{batch.call.uuid}/manage/?tab=reviewer-pool&pool_tab=assignments"
            ),
        }

        core_utils.broadcast_mail(
            "proposal",
            "assignment_batch_expired",
            context,
            manager_emails,
        )

        batch.manager_notified = True
        batch.save(update_fields=["manager_notified"])
        count += 1

    if count > 0:
        logger.info(f"Notified managers about {count} expired assignment batches")


@shared_task(name="waldur_mastermind.proposal.send_reviewer_invitation_email")
def send_reviewer_invitation_email(pool_member_uuid):
    pool_member = proposal_models.CallReviewerPool.objects.select_related(
        "call", "invited_by"
    ).get(uuid=pool_member_uuid)

    if not pool_member.invited_email:
        logger.warning(
            f"Cannot send reviewer invitation email. Pool member {pool_member_uuid} has no invited_email."
        )
        return

    invitation_link = core_utils.format_homeport_link(
        f"reviewer-invitation/{pool_member.invitation_token}/"
    )
    invited_by_name = (
        pool_member.invited_by.full_name if pool_member.invited_by else config.SITE_NAME
    )

    context = {
        "site_name": config.SITE_NAME,
        "call_name": pool_member.call.name,
        "invited_by_name": invited_by_name,
        "invitation_link": invitation_link,
    }

    core_utils.broadcast_mail(
        "proposal",
        "reviewer_invitation",
        context,
        [pool_member.invited_email],
    )
