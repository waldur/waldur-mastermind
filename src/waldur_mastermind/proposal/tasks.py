import dataclasses
import logging
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from celery import shared_task
from django.db import transaction
from django.db.models import Avg, Count, Exists, OuterRef, Q
from django.utils import timezone

from waldur_core.configuration import config
from waldur_core.core import utils as core_utils
from waldur_core.core.models import User
from waldur_core.core.service_access import names_calls
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import (
    activity,
    change_requests,
    contributions,
    mail_capture,
    notification_overrides,
    notification_rules,
    proposal_roles,
    provider_calls,
    round_reports,
    runs,
    step_inputs,
    utils,
    workflow_scenarios,
    workflow_service,
)
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal import status_labels as step_status_labels
from waldur_mastermind.proposal.enums import (
    REVIEW_SCORE_MAX,
    CallStates,
    ChangeRequestParts,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    ReviewerPoolInvitationStatuses,
    ReviewRejectionCauses,
    StepContributionStatuses,
    WorkflowPhases,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.serializers import (
    ProposalWorkflowStepInstanceSerializer,
    get_concealed_applicant_attributes,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Mail:
    """One mail a notification would send, built apart from sending it, so a
    dry run of the workflow can render what the real path delivers.

    ``audience`` is who it is for, as the notification rule recipient or the
    actor kind naming them (``applicant``, ``call_managers``, ...), and
    ``audience_detail`` what narrows it: the evaluator role, or the step's
    completer for a ``responsible_role`` rule.
    """

    event_type: str
    context: dict
    recipients: list
    audience: str
    audience_detail: str = ""
    single_message: bool = False
    template_variant: str | None = None
    app: str = "proposal"
    # Copied on the one message: on mail to the applicant, the holders of
    # the proposal roles the call copies (``Call.applicant_cc_roles``).
    cc: list = dataclasses.field(default_factory=list)
    # What the notification-sent event logs once the mail went out: the
    # proposal, template, trigger, audience and step (no address). Mails of
    # one audience share the dict, and are logged once with their total.
    log: dict | None = dataclasses.field(default=None, compare=False)
    # The call the mail is about: its own wording of the mail, if any, is
    # sent instead of the portal's (``notification_overrides``).
    call: Any = dataclasses.field(default=None, compare=False)


def deliver(mail: Mail) -> int:
    """Send a built mail, in its call's own wording where the call has one;
    how many recipients it reached."""
    reached = core_utils.broadcast_mail(
        mail.app,
        mail.event_type,
        mail.context,
        mail.recipients,
        template_variant=mail.template_variant,
        single_message=mail.single_message,
        cc=mail.cc or None,
        rendered=notification_overrides.call_override_render(mail),
    )
    return len(reached)


def deliver_all(mails):
    """Send built mails and log each audience reached once, with how many
    recipients its mails reached."""
    reached = {}
    for mail in mails:
        # The email log rows it writes, kept on its event: the call team
        # opens the mail as sent from the proposal's timeline.
        with mail_capture.capture_email_logs() as written:
            sent = deliver(mail)
        if mail.log is not None:
            log, total, logged = reached.get(id(mail.log), (mail.log, 0, []))
            reached[id(mail.log)] = (log, total + (sent or 0), logged + written)
    for log, total, logged in reached.values():
        activity.log_notification_sent(
            log["proposal"],
            log["template"],
            log["trigger"],
            log["audience"],
            total,
            step_instance=log.get("step_instance"),
            email_log_uuids=logged,
        )


def _applicant_variant(event_type):
    """The template set an applicant's mail uses: a deployment that hides
    calls from its applicants (``names_calls``) words its proposals as access
    requests, in templates named ``access_request_*`` for the same
    notification."""
    if names_calls():
        return None
    return event_type.replace("proposal_", "access_request_", 1)


@shared_task(
    name="waldur_mastermind.proposal.proposals_for_ended_rounds_should_be_cancelled"
)
def proposals_for_ended_rounds_should_be_cancelled():
    """Move drafts of ended rounds on to their call's next round, or cancel them.

    Also starts the post-cut-off lifecycle (``evaluating``) of every ended round.
    """
    # Only drafts: a proposal submitted before the cutoff is reviewed after it,
    # so submitted and in-review proposals are left to the review workflow.
    date = timezone.now()
    ended_rounds = proposal_models.Round.objects.filter(
        cutoff_time__lt=date, proposal__state=ProposalStates.DRAFT
    ).distinct()
    failed_round_ids = set()
    for call_round in ended_rounds.select_related("call"):
        try:
            utils.carry_over_drafts(call_round, announce=announce_carried_over_drafts)
        except Exception:
            # The move, its events and the announcement share one transaction,
            # so nothing moved. Cancelling here would throw away drafts the
            # call may carry over; they wait for the next sweep instead.
            logger.exception(
                "Failed to move the drafts of round %s on; retrying on the next run",
                call_round.uuid,
            )
            failed_round_ids.add(call_round.pk)

    cancellation_date = date.strftime("%Y-%m-%d %H:%M:%S")
    for proposal in (
        proposal_models.Proposal.objects.filter(
            state=ProposalStates.DRAFT, round__cutoff_time__lt=date
        )
        .exclude(round_id__in=failed_round_ids)
        .select_related(
            # Each save publishes a proposal event whose scope chain walks these.
            "round__call__manager__customer"
        )
    ):
        proposal.state = ProposalStates.CANCELED
        proposal.save(update_fields=["state"])

        event_logger.emit(
            "Proposal {proposal_name} has been canceled.",
            event_type=EventType.PROPOSAL_CANCELED,
            event_context={"proposal": proposal},
            scopes=[proposal],
        )
        logger.info(f"Proposal {proposal.name} has been canceled.")

        # Schedule notification to the proposal creator
        notify_proposal_creator_about_cancelled_proposal.apply_async(
            args=(proposal.uuid, cancellation_date),
            countdown=10,  # 10 second delay
        )

    utils.start_evaluation_of_ended_rounds()


def announce_carried_over_drafts(moved, previous_round):
    """Tell each applicant once that their draft now targets another cut-off."""
    for proposal in moved:
        transaction.on_commit(
            lambda proposal_uuid=proposal.uuid: (
                notify_proposal_creator_about_carried_over_draft.delay(
                    proposal_uuid, previous_round.uuid
                )
            )
        )


@shared_task(
    name="waldur_mastermind.proposal.notify_proposal_creator_about_carried_over_draft"
)
def notify_proposal_creator_about_carried_over_draft(
    proposal_uuid, previous_round_uuid
):
    proposal = proposal_models.Proposal.objects.select_related(
        "round__call", "created_by"
    ).get(uuid=proposal_uuid)
    if not proposal.created_by or not proposal.created_by.email:
        logger.warning(
            "Cannot tell the creator of proposal %s that it moved to another "
            "round: they have no email.",
            proposal.uuid,
        )
        return
    previous_round = proposal_models.Round.objects.filter(
        uuid=previous_round_uuid
    ).first()

    context = {
        "site_name": config.SITE_NAME,
        "proposal_creator_name": proposal.created_by.full_name,
        "proposal_name": proposal.name,
        "call_name": proposal.round.call.name,
        "previous_round_name": previous_round.name if previous_round else "",
        "round_name": proposal.round.name,
        "round_start_date": proposal.round.start_time,
        "deadline_date": proposal.round.cutoff_time or proposal_models.NO_CUTOFF_TEXT,
        "proposal_url": core_utils.format_homeport_link(
            "proposals/{proposal_uuid}/",
            proposal_uuid=proposal.uuid,
        ),
    }
    core_utils.broadcast_mail(
        "proposal",
        "proposal_draft_carried_over",
        context,
        [proposal.created_by.email],
        template_variant=_applicant_variant("proposal_draft_carried_over"),
        cc=notification_rules.applicant_cc_emails(
            proposal, [proposal.created_by.email]
        ),
    )


@shared_task(
    name="waldur_mastermind.proposal.run_call_workflow_scenarios",
    soft_time_limit=workflow_scenarios.CHECK_RUN_SOFT_LIMIT_SECONDS,
    time_limit=workflow_scenarios.CHECK_RUN_SOFT_LIMIT_SECONDS + 60,
)
def run_call_workflow_scenarios(call_uuid):
    """Dry-run the saved scenarios of a call's template on the call and keep
    the results on the call's latest version."""
    call = proposal_models.Call.objects.filter(uuid=call_uuid).first()
    if call is None:
        return
    workflow_scenarios.run_call_scenarios(call)


@shared_task(name="waldur_mastermind.proposal.expired_reviews_should_be_cancelled")
def expired_reviews_should_be_cancelled():
    """Cancel reviews past their deadline (``Review.review_end_date``): the
    deadline of the step they contribute to, else the round's review
    duration."""
    for review in proposal_models.Review.objects.filter(
        state=proposal_models.Review.States.IN_REVIEW
    ).select_related(
        "proposal__round", "assignment_item__batch", "contribution__proposal_step"
    ):
        review_end_date = review.review_end_date
        if review_end_date and review_end_date <= timezone.now():
            review.state = proposal_models.Review.States.REJECTED
            review.rejection_cause = ReviewRejectionCauses.EXPIRED
            review.save(update_fields=["state", "rejection_cause"])

            activity.log_review_event(review, EventType.REVIEW_CANCELED)
            logger.info(f"Review {review.proposal.name} has been canceled.")


@shared_task(name="waldur_mastermind.proposal.notify_user_about_proposal_state_update")
def notify_user_about_proposal_state_update(proposal_uuid, previous_state, new_state):
    proposal = proposal_models.Proposal.objects.get(uuid=proposal_uuid)
    deliver_all(proposal_state_update_mails(proposal, previous_state, new_state))


def proposal_state_update_mails(proposal, previous_state, new_state):
    """The mail telling the proposal's creator its state changed."""
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
    allocation_date = None
    granted_duration = None
    # The guard replaces a bare `except AttributeError`, which existed only to
    # swallow `proposal.project` being None and hid every other attribute error
    # with it.
    if new_state == ProposalStates.ACCEPTED and proposal.project:
        project_link = core_utils.format_homeport_link(
            "projects/{project_uuid}/",
            project_uuid=proposal.project.uuid,
        )
        # The day the grant starts running: the project's own start where the
        # call dates allocation forward, otherwise the day it was created.
        allocation_date = proposal.project.start_date or timezone.localdate(
            proposal.project.created
        )
        granted_duration = utils.granted_duration_in_days(proposal)
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
        "update_date": proposal.modified,
        # Declared on the context model and rendered by both bodies since day
        # one, but never actually passed — the line shipped blank until now.
        "allocation_date": allocation_date,
        "rejection_feedback": proposal.allocation_comment,
        "allocated_resources": allocated_resources
        if new_state == ProposalStates.ACCEPTED
        else None,
    }

    # A separate set of templates rather than conditionals threaded through
    # one: a deployment that hides calls from applicants sends a different
    # message, and each set can be reworded and overridden without disturbing
    # the other. Still one event and one notification, so an operator has a
    # single switch for "tell the applicant their request changed state" —
    # a deployment is in one mode and only ever sends one of the two.
    template_variant = None
    if names_calls():
        # Only the call-managed wording names these, so only it is handed them.
        context["call_name"] = proposal.round.call.name
        context["review_period"] = proposal.round.review_duration_in_days
        # Unit included, and None when nothing is known: the applicant is no
        # longer asked for a duration, so the template guards the line.
        context["duration"] = utils.requested_duration_label(proposal)
    else:
        template_variant = "access_request_state_changed"
        # Nothing asks a marketplace applicant for a duration, so the only
        # honest figure is the one they were granted — and None where the grant
        # does not expire, which that wording's template omits.
        context["duration"] = granted_duration

    recipients = (
        [proposal.created_by.email]
        if proposal.created_by and proposal.created_by.email
        else []
    )
    return [
        Mail(
            "proposal_state_changed",
            context,
            recipients,
            cc=notification_rules.applicant_cc_emails(proposal, recipients),
            audience="applicant",
            template_variant=template_variant,
            call=proposal.round.call,
            log={
                "proposal": proposal,
                "template": "proposal_state_changed",
                "trigger": new_state,
                "audience": NotificationRuleRecipients.APPLICANT,
            },
        )
    ]


@shared_task(
    name="waldur_mastermind.proposal.notify_call_managers_about_new_proposal_submission"
)
def notify_call_managers_about_new_proposal_submission(proposal_uuid):
    proposal = proposal_models.Proposal.objects.get(uuid=proposal_uuid)
    # Logged and kept as sent, as the applicant's mail of the same
    # submission is: the call team reads both on the proposal's timeline.
    deliver_all(new_submission_mails(proposal))


def new_submission_mails(proposal):
    """The mail telling the call's managers a proposal was submitted."""
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
    return [
        Mail(
            "new_proposal_submitted",
            context,
            recipients,
            audience="call_managers",
            call=proposal.round.call,
            log={
                "proposal": proposal,
                "template": "new_proposal_submitted",
                "trigger": ProposalStates.SUBMITTED,
                "audience": NotificationRuleRecipients.CALL_MANAGERS,
            },
        )
    ]


@shared_task(
    name="waldur_mastermind.proposal.notify_call_managers_about_round_adoption"
)
def notify_call_managers_about_round_adoption(round_uuid, user_uuid):
    """Tell the call's managers its round's results were adopted.

    Only the date and who recorded it: the note and document stay on the
    round, where only those who may see held decisions read them.
    """
    call_round = proposal_models.Round.objects.select_related("call").get(
        uuid=round_uuid
    )
    recipients = list(
        call_round.call.call_managers.exclude(email="").values_list("email", flat=True)
    )
    if not recipients:
        return
    user = User.all_objects.filter(uuid=user_uuid).first()
    context = {
        "site_name": config.SITE_NAME,
        "call_name": call_round.call.name,
        "round_name": call_round.name,
        "adopted_at": call_round.adopted_at,
        "recorded_by": (user.full_name or user.username) if user else "",
        "round_url": core_utils.format_homeport_link(
            "call/{call_uuid}/edit/?tab=rounds",
            call_uuid=call_round.call.uuid.hex,
        ),
    }
    core_utils.broadcast_mail(
        "proposal",
        "round_results_adopted",
        context,
        recipients,
    )


def _round_report_url(report):
    return core_utils.format_homeport_link(
        "round-reports/{report_uuid}/", report_uuid=report.uuid.hex
    )


@shared_task(name="waldur_mastermind.proposal.notify_round_report_authors")
def notify_round_report_authors(report_uuids, days_left=None):
    """Ask each author of a draft round report for it: when the round step
    opens, and ``days_left`` days before it is due."""
    reports = (
        proposal_models.RoundMeetingNote.objects.filter(
            uuid__in=report_uuids, submitted_at__isnull=True
        )
        .exclude(author=None)
        .select_related("round__call", "call_step", "author")
    )
    deliver_all(round_report_mails(reports, days_left=days_left))


def round_report_mails(reports, days_left=None):
    """The mail asking each author of ``reports`` for their round report.

    The mail counts the proposals the report covers and links to it; it
    names none of them, as the round's applicants may be undisclosed to the
    author until they open the report.
    """
    mails = []
    for report in reports:
        author, call_step = report.author, report.call_step
        if author is None or call_step is None or not author.email:
            continue
        round_step = round_reports.round_step_of(report.round, call_step)
        context = {
            "site_name": config.SITE_NAME,
            "call_name": report.round.call.name,
            "round_name": report.round.name,
            "step_name": call_step.name or call_step.key,
            "proposals_count": report.proposals.count(),
            "due_at": round_step.due_at if round_step else None,
            "days_left": days_left,
            "report_url": _round_report_url(report),
        }
        mails.append(
            Mail(
                "round_report_requested",
                context,
                [author.email],
                audience="round_report_author",
                call=report.round.call,
            )
        )
    return mails


@shared_task(name="waldur_mastermind.proposal.notify_call_managers_about_round_step")
def notify_call_managers_about_round_step(round_step_uuid):
    """Tell the call's managers a round step was opened, with how many
    reports were asked for."""
    round_step = proposal_models.RoundStep.objects.select_related(
        "round__call", "call_step", "opened_by"
    ).get(uuid=round_step_uuid)
    deliver_all(round_step_opened_mails(round_step))


def round_step_opened_mails(round_step):
    """The mail telling the call's managers a round step was opened."""
    call_round = round_step.round
    recipients = list(
        call_round.call.call_managers.exclude(email="").values_list("email", flat=True)
    )
    if not recipients:
        return []
    opened_by = round_step.opened_by
    context = {
        "site_name": config.SITE_NAME,
        "call_name": call_round.call.name,
        "round_name": call_round.name,
        "step_name": round_step.call_step.name or round_step.call_step.key,
        "opened_by": (opened_by.full_name or opened_by.username) if opened_by else "",
        "reports_count": round_reports.reports_of(call_round, round_step.call_step)
        .exclude(proposals=None)
        .count(),
        "proposals_count": round_reports.instances_at(
            call_round, round_step.call_step
        ).count(),
        "due_at": round_step.due_at,
        "round_url": core_utils.format_homeport_link(
            "call/{call_uuid}/edit/?tab=rounds", call_uuid=call_round.call.uuid.hex
        ),
    }
    return [
        Mail(
            "round_step_opened",
            context,
            recipients,
            audience="call_managers",
            call=call_round.call,
        )
    ]


# Lead times, in days before a round step is due, of the reminders to its
# report authors.
ROUND_REPORT_REMINDER_DAYS = (7, 1)
ROUND_REPORT_REMINDER_PREFIX = "reminder:"


@shared_task(name="waldur_mastermind.proposal.send_round_report_reminders")
def send_round_report_reminders():
    """Remind the authors of draft round reports of a step due in
    ``ROUND_REPORT_REMINDER_DAYS`` days, once per lead time, recorded in the
    round step's ledger. Returns how many reports were reminded of."""
    now = timezone.now()
    sent = 0
    round_steps = proposal_models.RoundStep.objects.filter(
        opened_at__isnull=False,
        due_at__gt=now,
        due_at__lte=now + timedelta(days=max(ROUND_REPORT_REMINDER_DAYS) + 1),
        call_step__round_scoped=True,
    )
    for round_step in round_steps:
        due_at = round_step.due_at
        if due_at is None:
            continue
        days_left = (due_at.date() - now.date()).days
        if days_left not in ROUND_REPORT_REMINDER_DAYS:
            continue
        key = f"{ROUND_REPORT_REMINDER_PREFIX}{days_left}"
        with transaction.atomic():
            locked = proposal_models.RoundStep.objects.select_for_update().get(
                pk=round_step.pk
            )
            if key in locked.sent_notifications:
                continue
            drafts = [
                uuid.hex
                for uuid in round_reports.reports_of(locked.round, locked.call_step)
                .filter(submitted_at__isnull=True)
                .exclude(proposals=None)
                .values_list("uuid", flat=True)
            ]
            locked.sent_notifications = [*locked.sent_notifications, key]
            locked.save(update_fields=["sent_notifications"])
        if drafts:
            notify_round_report_authors.delay(drafts, days_left)
            sent += len(drafts)
    return sent


@shared_task(
    name="waldur_mastermind.proposal.notify_call_managers_about_withdrawn_proposal"
)
def notify_call_managers_about_withdrawn_proposal(proposal_uuid, user_uuid, reason=""):
    proposal = proposal_models.Proposal.objects.select_related(
        "round__call__manager__customer"
    ).get(uuid=proposal_uuid)
    user = User.objects.filter(uuid=user_uuid).first()
    context = {
        "site_name": config.SITE_NAME,
        "proposal_url": core_utils.format_homeport_link(
            "call-management/{customer_uuid}/proposals/{proposal_uuid}/",
            customer_uuid=proposal.round.call.manager.customer.uuid,
            proposal_uuid=proposal.uuid,
        ),
        "proposal_name": proposal.name,
        "withdrawn_by": (user.full_name or user.username) if user else "",
        "reason": reason,
        "call_name": proposal.round.call.name,
        "round_name": proposal.round.name,
    }
    recipients = list(proposal.round.call.call_managers.values_list("email", flat=True))
    core_utils.broadcast_mail("proposal", "proposal_withdrawn", context, recipients)


def _call_manager_emails_off_team(proposal):
    """Addresses of the call's managers to tell about the proposal's reviews:
    reviewer names and scores are evaluation detail, which nobody on the
    proposal's applicant team hears of, whatever role they hold on the call."""
    return list(
        proposal.round.call.call_managers.exclude(
            id__in=proposal_roles.applicant_team_user_ids(proposal)
        )
        .exclude(email="")
        .values_list("email", flat=True)
    )


def criterion_scores(review) -> list[dict]:
    """The review's score on each criterion of its step, for a mail:
    ``{name, weight, score}`` in the criteria's order, score None when unrated.
    Empty when the step has no criteria."""
    scores = review.criteria_scores or {}
    return [
        {
            "name": criterion.name,
            "weight": criterion.weight,
            "score": scores.get(contributions.criterion_key(criterion)),
        }
        for criterion in contributions.review_criteria(review)
    ]


@shared_task(name="waldur_mastermind.proposal.notify_call_managers_about_new_review")
def notify_call_managers_about_new_review(review_uuid):
    review = proposal_models.Review.objects.get(uuid=review_uuid)
    review_counts = utils.get_proposal_review_counts(review.proposal)

    context = {
        "site_name": config.SITE_NAME,
        "review_url": core_utils.format_homeport_link(
            "proposal-review/{review_uuid}/",
            review_uuid=review.uuid,
        ),
        "proposal_name": review.proposal.name,
        "call_name": review.proposal.round.call.name,
        "reviewer_name": review.reviewer.full_name,
        "submission_date": review.modified,
        "score": review.summary_score,
        "max_score": str(REVIEW_SCORE_MAX),
        "criteria": criterion_scores(review),
        **review_counts,
    }

    recipients = _call_manager_emails_off_team(review.proposal)
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

    recipients = _call_manager_emails_off_team(review.proposal)
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
        template_variant=_applicant_variant("proposal_cancelled"),
        cc=notification_rules.applicant_cc_emails(proposal, [proposal.created_by.email])
        if proposal.created_by and proposal.created_by.email
        else None,
    )


def _creator_name_for(reader, proposal):
    """The applicant's name as ``reader`` sees it on the proposal: left out
    when the call's applicant visibility config conceals it from them."""
    if not proposal.created_by:
        return "N/A"
    concealed = get_concealed_applicant_attributes(reader, proposal) or ()
    if "full_name" in concealed:
        return ""
    return proposal.created_by.full_name


@shared_task(
    name="waldur_mastermind.proposal.notify_reviewer_about_assignment",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,  # Max 10 minutes between retries
    max_retries=3,
)
def notify_reviewer_about_assignment(review_uuid):
    review = proposal_models.Review.objects.get(uuid=review_uuid)
    for mail in review_assigned_mails(review):
        deliver(mail)


def review_assigned_mails(review):
    """The mail telling a reviewer a review was assigned to them."""
    if not review.reviewer or not review.reviewer.email:
        logger.warning(
            f"Cannot send review assignment notification. Review {review.uuid} reviewer has no valid email."
        )
        return []

    link_to_reviews_list = core_utils.format_homeport_link(
        "reviews/",
    )

    context = {
        "site_name": config.SITE_NAME,
        "reviewer_name": review.reviewer.full_name,
        "call_name": review.proposal.round.call.name,
        "proposal_name": review.proposal.name,
        "proposal_creator_name": _creator_name_for(review.reviewer, review.proposal),
        "submission_date": review.proposal.created,
        "review_deadline": review.review_end_date,
        "link_to_reviews_list": link_to_reviews_list,
    }

    return [
        Mail(
            "review_assigned",
            context,
            [review.reviewer.email],
            audience="reviewers",
            call=review.proposal.round.call,
        )
    ]


@shared_task(
    name="waldur_mastermind.proposal.notify_reviewer_on_review_deadline_approaching"
)
def notify_reviewer_on_review_deadline_approaching():
    now = timezone.now()
    reviews = proposal_models.Review.objects.filter(
        state=proposal_models.Review.States.IN_REVIEW,
        proposal__round__call__state=CallStates.ACTIVE,
    ).select_related(
        "reviewer",
        "proposal",
        "proposal__round",
        "proposal__round__call",
        "contribution__proposal_step",
    )

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
    for mail in reviewer_decision_mails(proposal):
        deliver(mail)


def reviewer_decision_mails(proposal):
    """The mail telling each reviewer of the proposal it was decided."""
    mails = []
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

            mails.append(
                Mail(
                    "proposal_decision_for_reviewer",
                    context,
                    [review.reviewer.email],
                    audience="reviewers",
                    call=proposal.round.call,
                )
            )
        else:
            logger.warning(
                f"Cannot send proposal decision notification to reviewer for review {review.uuid}. Reviewer has no valid email."
            )
    return mails


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


def notify_contribution_decision(proposal, completed, next_instance):
    """Tell the applicant when contributions completed the workflow: the last
    one submitted, or the last open one taken out of the step's work."""
    proposal.refresh_from_db()
    if (
        completed
        and next_instance is None
        and not proposal.decision_held
        and proposal.state != ProposalStates.IN_REVIEW
        # A post-award run ended: the proposal was accepted long before, and
        # runs exist only after that.
        and not proposal.runs.exists()
    ):
        notify_proposal_decision(
            proposal.uuid, ProposalStates.IN_REVIEW, proposal.state
        )


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


@shared_task(name="waldur_mastermind.proposal.notify_provider_about_offering_request")
def notify_provider_about_offering_request(requested_offering_uuid):
    """Tell the provider side that a call requests one of its offerings: the
    offering's managers, who may answer it, and the provider organisation's
    owners. Where the call's workflow hands work to the offering's team and
    the offering has no manager, the mail asks for one: only the provider can
    add them."""
    requested_offering = (
        proposal_models.RequestedOffering.objects.filter(
            uuid=requested_offering_uuid,
            state=proposal_models.RequestedOffering.States.REQUESTED,
        )
        .select_related("call__manager__customer", "offering__customer", "created_by")
        .first()
    )
    if requested_offering is None:
        return
    for mail in offering_request_mails(requested_offering):
        deliver(mail)


def offering_request_mails(requested_offering):
    """The mail telling the provider side of a new offering request."""
    offering = requested_offering.offering
    provider = offering.customer
    managers = get_users(offering, role_name=RoleEnum.OFFERING_MANAGER)
    people = list(managers)
    if provider is not None:
        people += list(get_users(provider, role_name=RoleEnum.CUSTOMER_OWNER))
    recipients = list(
        dict.fromkeys(
            user.email for user in people if user.email and user.notifications_enabled
        )
    )
    if not recipients:
        return []
    call = requested_offering.call
    steps = provider_calls.team_steps(call)
    creator = requested_offering.created_by
    context = {
        "site_name": config.SITE_NAME,
        "offering_name": offering.name,
        "provider_name": provider.name if provider else "",
        "call_name": call.name,
        "call_managing_organisation": call.manager.customer.name,
        "requested_by": (creator.full_name or creator.username) if creator else "",
        "message": requested_offering.description or "",
        "team_steps": ", ".join(step.name for step in steps),
        "needs_offering_managers": bool(steps) and not managers.exists(),
        "requests_url": core_utils.format_homeport_link(
            "providers/{provider_uuid}/offering-requests/",
            provider_uuid=provider.uuid.hex if provider else "",
        ),
        "team_url": core_utils.format_homeport_link(
            "providers/{provider_uuid}/marketplace-provider-offering-details/"
            "{offering_uuid}/?tab=permissions",
            provider_uuid=provider.uuid.hex if provider else "",
            offering_uuid=offering.uuid.hex,
        ),
    }
    return [
        Mail(
            "requested_offering_created",
            context,
            recipients,
            audience="provider",
            call=call,
        )
    ]


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
        # A provider declining a request is a rejection, whatever the state
        # is called.
        "decision": (
            "rejected"
            if requested_offering.state == RequestedOfferingStates.CANCELED
            else requested_offering.state
        ),
        "decision_date": requested_offering.modified.strftime("%B %d, %Y"),
        "call_url": call_url,
        "reason": requested_offering.rejection_reason,
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
            "end_date": round.cutoff_time or proposal_models.NO_CUTOFF_TEXT,
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

    carries_over: dict[int, bool] = {}
    for proposal in proposals:
        cutoff_time = proposal.round.cutoff_time
        if cutoff_time is None:
            # A round without a cut-off has no deadline to remind of.
            continue
        time_remaining = cutoff_time - now
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

        if proposal.round.pk not in carries_over:
            carries_over[proposal.round.pk] = (
                utils.next_round_for_drafts(proposal.round) is not None
            )

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
            "draft_carries_over": carries_over[proposal.round.pk],
        }

        core_utils.broadcast_mail(
            "proposal",
            "proposal_submission_deadline_approaching",
            context,
            [proposal.created_by.email],
            template_variant=_applicant_variant(
                "proposal_submission_deadline_approaching"
            ),
            cc=notification_rules.applicant_cc_emails(
                proposal, [proposal.created_by.email]
            ),
        )


@shared_task(
    name="waldur_mastermind.proposal.notify_manager_when_reviews_are_completed"
)
def notify_manager_when_reviews_are_completed(proposal_uuid):
    proposal = proposal_models.Proposal.objects.get(uuid=proposal_uuid)
    for mail in reviews_completed_mails(proposal):
        deliver(mail)


def reviews_completed_mails(proposal):
    """The mail telling the call's managers every review of the proposal is in."""
    completed_reviews = proposal.review_set.filter(
        state=proposal_models.Review.States.SUBMITTED
    )
    incomplete_reviews = proposal.review_set.filter(
        state=proposal_models.Review.States.IN_REVIEW
    )

    # The "enough reviewers" threshold is sourced from the call's review step
    # (single config track), not the removed Round field.
    review_step = proposal_models.CallWorkflowStep.review_step_for(proposal.round.call)
    min_reviewers = review_step.min_contributions if review_step else None

    if incomplete_reviews.exists() or completed_reviews.count() < (min_reviewers or 0):
        return []

    call = proposal.round.call
    manager_emails = _call_manager_emails_off_team(proposal)

    if not manager_emails:
        logger.warning(
            f"Cannot send review completion notification. Call {call.uuid} has no managers with valid emails."
        )
        return []

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
                "criteria": criterion_scores(review),
                "submitted_at": review.modified,
            }
            for review in completed_reviews
        ],
        "proposal_url": proposal_url,
    }

    return [
        Mail(
            "reviews_complete",
            context,
            manager_emails,
            audience="call_managers",
            call=call,
        )
    ]


@shared_task(name="waldur_mastermind.proposal.start_evaluation_for_closed_rounds")
def start_evaluation_for_closed_rounds():
    """Start the evaluation of proposals held for their round's cut-off.

    A call that evaluates at the cut-off leaves each proposal ``submitted``
    with its steps pending. Once the round's cut-off has passed, this activates
    the first pending step of every such proposal, one transaction per
    proposal so a single failure does not hold back the rest of the batch.

    The candidates are not filtered on the call's current setting: only a
    held submission leaves a proposal ``submitted`` with a pending step, so a
    proposal held just before a manager switched the call back is still
    started rather than stranded. Idempotent: a started proposal is
    ``in_review`` and is not picked up again.

    The rounds whose cut-off has passed enter ``evaluating`` first, so the
    batch starts under a round that says it is being evaluated.
    """
    utils.start_evaluation_of_ended_rounds()
    return start_held_proposals()


def start_held_proposals(call_round=None):
    """Activate the first step of each proposal held for its round's cut-off.

    Every round whose cut-off has passed, or only ``call_round``: closing a
    round early starts its proposals at once instead of leaving them for the
    next sweep. Returns how many were started.
    """
    now = timezone.now()
    held = proposal_models.Proposal.objects.filter(
        state=ProposalStates.SUBMITTED,
        round__cutoff_time__lte=now,
        workflow_step_instances__status=WorkflowStepInstanceStatuses.PENDING,
    )
    if call_round is not None:
        held = held.filter(round=call_round)
    held_ids = list(
        held.exclude(
            workflow_step_instances__status=WorkflowStepInstanceStatuses.ACTIVE
        )
        .distinct()
        .values_list("id", flat=True)
    )

    started = []
    for proposal_id in held_ids:
        try:
            with transaction.atomic():
                proposal = (
                    proposal_models.Proposal.objects.select_for_update()
                    .filter(id=proposal_id, state=ProposalStates.SUBMITTED)
                    .first()
                )
                if proposal is None:
                    continue
                if workflow_service.activate_first_step(proposal) is None:
                    continue
        except Exception:
            logger.exception(
                "Failed to start the evaluation of proposal id=%s", proposal_id
            )
            continue
        started.append(proposal.uuid)

    for proposal_uuid in started:
        notify_user_about_proposal_state_update.delay(
            proposal_uuid, ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW
        )

    if started:
        logger.info("Started the evaluation of %d proposal(s)", len(started))
    return len(started)


def _waits_on_applicant_changes():
    """Steps waiting on the changes asked of their applicant: they do not
    lapse meanwhile, as it is not their actor who is late."""
    return Exists(
        change_requests.open_requests().filter(
            step_instance=OuterRef("pk"), contribution__isnull=True
        )
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
        )
        .exclude(_waits_on_applicant_changes())
        # A step run once per round ends for the round, by its reports or
        # its managers; its due date does not move proposals on one by one.
        .exclude(call_step__round_scoped=True)
        # A final report past its due date stays to be submitted; its lapse
        # was told once.
        .exclude(sent_notifications__contains=[workflow_service.OVERDUE_LEDGER_KEY])
        .values_list("id", flat=True)
    )

    expired_count = 0
    for instance_id in overdue_ids:
        try:
            with transaction.atomic():
                # The proposal first, then the step: the order the step
                # actions and the round's publication take, so an expiry
                # cannot deadlock against them (the decision step goes on to
                # lock the round).
                proposal_id = (
                    proposal_models.ProposalWorkflowStepInstance.objects.filter(
                        id=instance_id
                    )
                    .values_list("proposal_id", flat=True)
                    .first()
                )
                if proposal_id is None:
                    continue
                locked = proposal_models.Proposal.objects.select_for_update().get(
                    pk=proposal_id
                )
                instance = (
                    proposal_models.ProposalWorkflowStepInstance.objects.select_for_update()
                    .filter(
                        id=instance_id,
                        status=WorkflowStepInstanceStatuses.ACTIVE,
                        deadline__lt=timezone.now(),
                    )
                    .exclude(_waits_on_applicant_changes())
                    .exclude(
                        sent_notifications__contains=[
                            workflow_service.OVERDUE_LEDGER_KEY
                        ]
                    )
                    .first()
                )
                if instance is None:
                    continue
                workflow_service.expire_step(instance)
                # A lapse can lead to an allocation decision the system takes
                # at once; its award is announced like any other. A post-award
                # run's lapse leaves an accepted proposal as it was.
                proposal = proposal_models.Proposal.objects.get(pk=proposal_id)
                if (
                    locked.state == ProposalStates.IN_REVIEW
                    and proposal.state == ProposalStates.ACCEPTED
                    and not proposal.decision_held
                ):
                    transaction.on_commit(
                        lambda proposal_uuid=proposal.uuid: notify_proposal_decision(
                            proposal_uuid,
                            ProposalStates.IN_REVIEW,
                            ProposalStates.ACCEPTED,
                        )
                    )
        except Exception:
            logger.exception(
                "Failed to expire workflow step instance id=%s", instance_id
            )
            continue

        expired_count += 1

    if expired_count:
        logger.info("Expired %d workflow step instance(s)", expired_count)
    return expired_count


@shared_task(name="waldur_mastermind.proposal.open_due_final_reports")
def open_due_final_reports():
    """Open the final report of each accepted proposal whose project reaches
    the end of its allocation -- its end date, an extension having moved it,
    less the closing's offset -- and has none yet. Each in its own
    transaction, so one failure does not hold back the rest."""
    opened = 0
    for proposal in runs.due_final_report_proposals():
        try:
            workflow_service.open_run(proposal, WorkflowPhases.CLOSING)
        except runs.RunError as exc:
            logger.info(
                "Final report of proposal %s not opened: %s", proposal.uuid.hex, exc
            )
            continue
        except Exception:
            logger.exception(
                "Failed to open the final report of proposal %s", proposal.uuid.hex
            )
            continue
        opened += 1
    if opened:
        logger.info("Opened %d final report(s)", opened)
    return opened


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


# Homeport's "My assignments" tab, where a reviewer answers assignment batches.
MY_ASSIGNMENTS_PATH = "reviews/assignments/"


def _get_batch_reviewer_contact(batch):
    """Email address and display name of the reviewer an assignment batch is for."""
    pool_entry = batch.reviewer_pool_entry
    if pool_entry.reviewer:
        user = pool_entry.reviewer.user
        return user.email, user.full_name
    if pool_entry.invited_user:
        user = pool_entry.invited_user
        return user.email, user.full_name
    return pool_entry.invited_email, pool_entry.invited_email


@shared_task(name="waldur_mastermind.proposal.send_assignment_batch_invitation")
def send_assignment_batch_invitation(batch_uuid):
    """Email a reviewer the proposals of an assignment batch that was just sent.

    Proposal details are disclosed exactly as the reviewer's assignment API
    discloses them (``utils.disclosed_proposal_fields``).
    """
    batch = proposal_models.AssignmentBatch.objects.select_related(
        "call",
        "reviewer_pool_entry__reviewer__user",
        "reviewer_pool_entry__invited_user",
    ).get(uuid=batch_uuid)

    reviewer_email, reviewer_name = _get_batch_reviewer_contact(batch)
    if not reviewer_email:
        logger.warning(
            f"Cannot send assignment invitation for batch {batch.uuid}: no reviewer email"
        )
        return

    # Same disclosure as the reviewer's assignment API, so the two cannot
    # drift: a COI-blocked proposal is listed by title only.
    disclosure = utils.proposal_disclosure_for_reviewer(batch.call)
    proposals = []
    for item in batch.items.select_related("proposal").order_by("proposal__name"):
        fields = utils.disclosed_proposal_fields(item, disclosure)
        proposals.append(
            {"name": fields["proposal_name"], "summary": fields["proposal_summary"]}
        )

    context = {
        "site_name": config.SITE_NAME,
        "reviewer_name": reviewer_name,
        "call_name": batch.call.name,
        "proposals": proposals,
        "items_count": len(proposals),
        "expires_at": batch.expires_at,
        "manager_notes": batch.manager_notes,
        "link": core_utils.format_homeport_link(MY_ASSIGNMENTS_PATH),
    }

    core_utils.broadcast_mail(
        "proposal",
        "reviewer_assignment_invitation",
        context,
        [reviewer_email],
    )


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
            # A batch past its deadline is for the expiry task, not a reminder.
            expires_at__gt=timezone.now(),
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
            reviewer_email, reviewer_name = _get_batch_reviewer_contact(batch)

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
                    "items_count": batch.items.count(),
                    "link": core_utils.format_homeport_link(MY_ASSIGNMENTS_PATH),
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

        _, reviewer_name = _get_batch_reviewer_contact(batch)

        context = {
            "site_name": config.SITE_NAME,
            "call_name": batch.call.name,
            "reviewer_name": reviewer_name,
            "items_count": batch.items.count(),
            "sent_at": batch.sent_at,
            "expired_at": batch.expires_at,
            "assignments_url": core_utils.format_homeport_link(
                f"call/{batch.call.uuid}/manage/?tab=reviewer-pool&pool_tab=assignment_batches"
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


@shared_task(name="waldur_mastermind.proposal.mark_expired_reviewer_pool_invitations")
def mark_expired_reviewer_pool_invitations():
    """Expire pending pool invitations past their date and tell who sent them."""
    candidates = proposal_models.CallReviewerPool.objects.filter(
        invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        invitation_expires_at__lt=timezone.now(),
    ).select_related("call", "invited_by", "reviewer__user", "invited_user")

    count = 0
    for invitation in candidates:
        # The conditional update is the once-only guard: an invitation that was
        # answered, re-sent or expired by a concurrent run in the meantime is
        # left alone and its inviter is not notified again.
        updated = proposal_models.CallReviewerPool.objects.filter(
            pk=invitation.pk,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            invitation_expires_at__lt=timezone.now(),
        ).update(
            invitation_status=ReviewerPoolInvitationStatuses.EXPIRED,
            modified=timezone.now(),
        )
        if not updated:
            continue
        count += 1
        _notify_reviewer_pool_invitation_expired(invitation)

    if count:
        logger.info("Expired %d reviewer pool invitation(s)", count)
    return count


def _notify_reviewer_pool_invitation_expired(invitation):
    call = invitation.call
    if invitation.invited_by and invitation.invited_by.email:
        recipients = [invitation.invited_by.email]
    else:
        recipients = sorted(
            {email for email in call.call_managers.values_list("email", flat=True)}
            - {""}
        )
    if not recipients:
        logger.warning(
            "Cannot notify about expired reviewer pool invitation %s: "
            "nobody to notify for call %s",
            invitation.uuid,
            call.uuid,
        )
        return

    context = {
        "site_name": config.SITE_NAME,
        "call_name": call.name,
        "invitee_name": invitation.invitee_name,
        "invited_at": invitation.invited_at,
        "expired_at": invitation.invitation_expires_at,
        "reviewer_pool_url": core_utils.format_homeport_link(
            f"call/{call.uuid}/manage/?tab=reviewer-pool"
        ),
    }
    core_utils.broadcast_mail(
        "proposal",
        "reviewer_pool_invitation_expired",
        context,
        recipients,
    )


@shared_task(name="waldur_mastermind.proposal.send_reviewer_invitation_email")
def send_reviewer_invitation_email(pool_member_uuid, invitation_message=""):
    pool_member = proposal_models.CallReviewerPool.objects.select_related(
        "call", "invited_by", "reviewer__user", "invited_user"
    ).get(uuid=pool_member_uuid)

    if pool_member.reviewer:
        recipient = pool_member.reviewer.user.email
    elif pool_member.invited_user:
        recipient = pool_member.invited_user.email
    else:
        recipient = pool_member.invited_email

    if not recipient:
        logger.warning(
            f"Cannot send reviewer invitation email. Pool member {pool_member_uuid} has no email address."
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
        "invitation_message": invitation_message or "",
    }

    core_utils.broadcast_mail(
        "proposal",
        "reviewer_invitation",
        context,
        [recipient],
    )


def _addresses_evaluator_role_holders(rule):
    """Whether the rule's audience is the holders of an evaluator role, who
    open the proposal by itself rather than through the call's management."""
    if rule.recipient in NotificationRuleRecipients.NAMING_PROPOSAL_ROLE:
        return True
    return (
        rule.recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE
        and rule.workflow_step.completer == ResponsibleRoles.PROPOSAL_ROLE
    )


def _step_outcome_for(user, instance):
    """The step's outcome and reason as ``user`` reads them through the
    proposal's ``workflow_states`` endpoint, so that mail tells no more.

    Nothing to someone who does not see the proposal at all, such as a pool
    reviewer holding no work on it. Otherwise as
    ``ProposalWorkflowStepInstanceSerializer.to_representation`` masks them:
    a held decision is told only to those who may see held decisions; a
    step the call keeps from the applicant is not told to anyone reading
    it from the applicant's side, a member of the applicant team who also
    sits on the call included; a
    review verdict is the call team's unless the call shows reviews to
    submitters; feedback withheld for its report's release is the call
    team's until the release, then also the applicant team's and the step's
    own holders'. The call team is whoever reads the proposal as it
    (``step_inputs.reads_as_call_team``), and a call reviewer or panel
    member on the steps they take part in
    (``permissions.user_can_view_internal_notes``).
    """
    proposal = instance.proposal
    if (
        not filter_queryset_for_user(proposal_models.Proposal.objects.all(), user)
        .filter(pk=proposal.pk)
        .exists()
    ):
        return None, ""
    if (
        instance.is_allocation_decision
        and proposal.decision_held
        and not proposal_permissions.user_can_view_held_decision(user, proposal)
    ):
        return None, ""
    if proposal_permissions.reads_as_reviewer_only(
        user, proposal
    ) and not contributions.takes_part_in_step(user, instance):
        return None, ""
    if ProposalWorkflowStepInstanceSerializer.hides_internal_step_from(
        user, instance
    ) and not (
        ProposalWorkflowStepInstanceSerializer._is_review_step(instance)
        and proposal.round.call.reviews_visible_to_submitters
    ):
        return None, ""
    call_team = step_inputs.reads_as_call_team(
        user, proposal
    ) or proposal_permissions.user_can_view_internal_notes(user, proposal, instance)
    if (
        ProposalWorkflowStepInstanceSerializer._is_review_step(instance)
        and not call_team
        and not proposal.round.call.reviews_visible_to_submitters
    ):
        return None, ""
    if (
        instance.feedback_withheld
        and not call_team
        and not (
            instance.released_at is not None
            and (
                proposal_roles.is_on_applicant_team(user, proposal)
                or contributions.holds_step_work(user, instance)
            )
        )
    ):
        return instance.outcome, ""
    if ProposalWorkflowStepInstanceSerializer.conceals_offering_step_reason_from(
        user, instance
    ):
        return instance.outcome, ""
    return instance.outcome, instance.outcome_reason


def _reads_step_counts(user, instance):
    """Whether the mail about a contribution submitted to ``instance`` tells
    ``user`` how far the step's work is: whoever sees the proposal and reads
    a label of the step other than the applicant's (``status_labels``).
    Counts never name anyone."""
    proposal = instance.proposal
    if (
        not filter_queryset_for_user(proposal_models.Proposal.objects.all(), user)
        .filter(pk=proposal.pk)
        .exists()
    ):
        return False
    audience = ProposalWorkflowStepInstanceSerializer.label_audience(user, instance)
    return audience not in (None, step_status_labels.APPLICANT)


def _addresses_offering_teams(rule):
    return rule.recipient == NotificationRuleRecipients.OFFERING_TEAMS


def _addresses_offering_team_members(rule, instance):
    """Whether the rule mails people as an offering's team: the offering
    teams, or the offering managers a step's responsible role names."""
    return _addresses_offering_teams(rule) or (
        rule.recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE
        and instance.call_step.completer == ResponsibleRoles.OFFERING_MANAGER
    )


def offering_team_proposal_url(proposal_uuid, provider_uuid):
    """The proposal as an offering's team opens it, in its provider's
    workspace."""
    return core_utils.format_homeport_link(
        "providers/{provider_uuid}/call-proposals/{proposal_uuid}/",
        provider_uuid=UUID(str(provider_uuid)).hex,
        proposal_uuid=UUID(str(proposal_uuid)).hex,
    )


def _step_event_context(
    instance,
    trigger,
    audience_is_applicant,
    days_before=None,
    evaluators=False,
    outcome=None,
    outcome_reason="",
    offering_teams=False,
    provider_uuid=None,
):
    proposal = instance.proposal
    call = proposal.round.call
    if provider_uuid:
        proposal_url = offering_team_proposal_url(proposal.uuid, provider_uuid)
    elif audience_is_applicant or evaluators or offering_teams:
        proposal_url = core_utils.format_homeport_link(
            "proposals/{proposal_uuid}/", proposal_uuid=proposal.uuid
        )
    else:
        proposal_url = core_utils.format_homeport_link(
            "call-management/{customer_uuid}/proposals/{proposal_uuid}/",
            customer_uuid=call.manager.customer.uuid,
            proposal_uuid=proposal.uuid,
        )
    names_calls_here = names_calls() or not audience_is_applicant
    return {
        "site_name": config.SITE_NAME,
        "trigger": trigger,
        "step_name": instance.call_step.name or instance.step,
        "proposal_name": proposal.name,
        "proposal_url": proposal_url,
        # A marketplace-only deployment never shows its applicants a call: their
        # mail names the access request and nothing of the call or round it
        # went through. The call team's mail keeps them.
        "names_calls": names_calls_here,
        "call_name": call.name if names_calls_here else None,
        "round_name": proposal.round.name if names_calls_here else None,
        "deadline": instance.deadline,
        "days_before": days_before,
        # Outcome and reason are evaluation detail: never shown to the applicant
        # side, whose mail is status-only, and to everyone else only as far as
        # the proposal's workflow states show them (see _step_outcome_for).
        "outcome": None if audience_is_applicant else outcome,
        "outcome_reason": "" if audience_is_applicant else outcome_reason,
        "is_applicant": audience_is_applicant,
        # The hosting entities are told which project the award was granted
        # to, so they know whom to give access. A link only: opening the
        # project still takes a role on it or on its resources.
        "project_name": proposal.project.name
        if offering_teams and proposal.project_id
        else None,
        "project_url": core_utils.format_homeport_link(
            "projects/{project_uuid}/", project_uuid=proposal.project.uuid.hex
        )
        if offering_teams and proposal.project_id
        else None,
    }


def _rule_log_audience(rule):
    """The role a rule addresses, as the notification-sent event names it:
    its recipient, and the evaluator role it names. Never a person."""
    if rule.proposal_role_id:
        return f"{rule.recipient}:{rule.proposal_role.key}"
    return rule.recipient


def _send_step_event(instance, trigger, rules, days_before=None):
    """One mail per rule audience. Recipients addressed by several rules get one copy.

    An applicant audience gets one message with every recipient in To, so the
    proposal team sees who was told; any other audience gets a private copy
    each. Each audience reached is logged with its role and count.
    """
    deliver_all(step_event_mails(instance, trigger, rules, days_before=days_before))


def _rule_audience(rule):
    """``(audience, detail)`` of a rule's mail: its recipient, narrowed by the
    evaluator role it names or by the completer it addresses."""
    if rule.recipient in NotificationRuleRecipients.NAMING_PROPOSAL_ROLE:
        return rule.recipient, rule.proposal_role.key if rule.proposal_role_id else ""
    if rule.recipient == NotificationRuleRecipients.RESPONSIBLE_ROLE:
        call_step = rule.workflow_step
        if call_step.completer == ResponsibleRoles.PROPOSAL_ROLE:
            role = call_step.completer_proposal_role
            return rule.recipient, f"proposal_role:{role.key if role else ''}"
        return rule.recipient, call_step.completer or ""
    return rule.recipient, ""


def step_event_mails(instance, trigger, rules, days_before=None):
    """The mails ``_send_step_event`` sends for ``rules``."""
    mails = []
    already_addressed = set()
    for rule in rules:
        users = notification_rules.resolve_recipients(rule, instance.proposal)
        emails = sorted(set(users.values_list("email", flat=True)) - already_addressed)
        if not emails:
            continue
        already_addressed.update(emails)
        is_applicant = notification_rules.is_applicant_audience(rule, instance.proposal)
        cc = []
        if is_applicant:
            cc = [
                email
                for email in notification_rules.applicant_cc_emails(
                    instance.proposal, emails
                )
                if email not in already_addressed
            ]
            already_addressed.update(cc)
        counted = trigger == NotificationRuleTriggers.CONTRIBUTION_SUBMITTED
        as_offering_team = _addresses_offering_team_members(rule, instance)
        if is_applicant:
            batches = {(None, "", False, None): emails}
        else:
            # Private copies each, grouped by what each recipient may read
            # of the outcome, and of the step's counts.
            batches = {}
            seen = set()
            for user in users.filter(email__in=emails).order_by("email", "id"):
                if user.email in seen:
                    continue
                seen.add(user.email)
                outcome = _step_outcome_for(user, instance)
                # An offering's team is linked to its own view of the
                # proposal, in its provider's workspace.
                provider = (
                    contributions.offering_team_provider_uuid(user, instance.proposal)
                    if as_offering_team
                    else None
                )
                key = (
                    *outcome,
                    counted and _reads_step_counts(user, instance),
                    provider,
                )
                batches.setdefault(key, []).append(user.email)
        log = {
            "proposal": instance.proposal,
            "template": "workflow_step_event",
            "trigger": trigger,
            "audience": _rule_log_audience(rule),
            "step_instance": instance,
        }
        for (outcome, outcome_reason, reads_counts, provider), batch in batches.items():
            context = _step_event_context(
                instance,
                trigger,
                is_applicant,
                days_before=days_before,
                evaluators=_addresses_evaluator_role_holders(rule),
                outcome=outcome,
                outcome_reason=outcome_reason,
                offering_teams=_addresses_offering_teams(rule),
                provider_uuid=provider,
            )
            if reads_counts:
                context["counts"] = contributions.step_counts(instance)
            audience, detail = _rule_audience(rule)
            mails.append(
                Mail(
                    "workflow_step_event",
                    context,
                    batch,
                    audience="applicant" if is_applicant else audience,
                    audience_detail="" if is_applicant else detail,
                    single_message=is_applicant,
                    cc=cc,
                    log=log,
                    call=instance.proposal.round.call,
                )
            )
    return mails


@shared_task(name="waldur_mastermind.proposal.notify_proposals")
def notify_proposals(proposal_uuids, recipients, subject, body):
    """Send a call manager's message: to the recipients of each proposal
    (``notification_rules.manager_message_emails``), re-read at send time."""
    proposals = proposal_models.Proposal.objects.filter(
        uuid__in=proposal_uuids
    ).select_related("round__call", "created_by")
    for proposal in proposals:
        to, cc = notification_rules.manager_message_emails(proposal, recipients)
        if not to:
            continue
        context = {
            "site_name": config.SITE_NAME,
            "subject": subject,
            "body": body,
            "proposal_name": proposal.name,
            "call_name": proposal.round.call.name,
            "proposal_url": core_utils.format_homeport_link(
                "proposals/{proposal_uuid}/", proposal_uuid=proposal.uuid
            ),
        }
        core_utils.broadcast_mail(
            "proposal",
            "proposal_manager_message",
            context,
            to,
            cc=cc or None,
        )


@shared_task(name="waldur_mastermind.proposal.notify_workflow_step_event")
def notify_workflow_step_event(instance_uuid, trigger):
    """Deliver a status-change event (started / completed / rejected / expired).

    Enqueued by ``notification_rules.dispatch_step_event`` after commit; the
    rules are re-read here so a rule disabled in the meantime is honoured.
    """
    instance = proposal_models.ProposalWorkflowStepInstance.objects.select_related(
        "proposal__round__call__manager__customer"
    ).get(uuid=instance_uuid)
    rules = list(notification_rules.enabled_rules(instance, trigger))
    if not rules:
        return
    _send_step_event(instance, trigger, rules)


@shared_task(name="waldur_mastermind.proposal.notify_proposal_role_changed")
def notify_proposal_role_changed(assignment_uuid, assigned):
    """Tell the holder an evaluator role on a proposal was given or taken away.

    Mail to the holder alone: holders are not the applicant team, and the
    applicant is never told who evaluates their proposal.
    """
    assignment = proposal_roles.get_assignment(assignment_uuid)
    if assignment is None:
        return
    deliver_all(proposal_role_mails(assignment, assigned))


def proposal_role_mails(assignment, assigned):
    """The mail telling a holder an evaluator role was given or taken away."""
    user = assignment.user
    if not user.is_active or not user.email or not user.notifications_enabled:
        return []
    proposal = assignment.proposal
    context = {
        "site_name": config.SITE_NAME,
        "holder_name": user.full_name or user.username,
        "role_name": assignment.role.name,
        "is_lead": assignment.is_lead,
        "proposal_name": proposal.name,
        "call_name": proposal.round.call.name,
        "proposal_url": core_utils.format_homeport_link(
            "proposals/{proposal_uuid}/", proposal_uuid=proposal.uuid.hex
        ),
        "coi_confirmation_required": assignment.awaits_coi_confirmation,
    }
    template = "proposal_role_assigned" if assigned else "proposal_role_revoked"
    return [
        Mail(
            template,
            context,
            [user.email],
            audience="proposal_role",
            audience_detail=assignment.role.key,
            call=proposal.round.call,
            log={
                "proposal": proposal,
                "template": template,
                "trigger": "role_assigned" if assigned else "role_revoked",
                "audience": (
                    f"{NotificationRuleRecipients.PROPOSAL_ROLE}:{assignment.role.key}"
                ),
            },
        )
    ]


class DelegationEvents:
    """What happened to a contribution slot handed to someone."""

    DELEGATED = "delegated"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    REVOKED = "revoked"

    ALL = (DELEGATED, ACCEPTED, DECLINED, REVOKED)


@shared_task(name="waldur_mastermind.proposal.notify_contribution_delegated")
def notify_contribution_delegated(contribution_uuid, event, recipient_uuid, actor_uuid):
    """Tell one person what happened to a slot handed on: the person it is
    handed to (``delegated``) or taken from (``revoked``), the one who handed
    it on when it is accepted or declined.

    Evaluators and providers only: the applicant is never told who
    evaluates their proposal.
    """
    if event not in DelegationEvents.ALL:
        return
    contribution = (
        proposal_models.StepContribution.objects.filter(uuid=contribution_uuid)
        .select_related(
            "proposal_step__call_step",
            "proposal_step__proposal__round__call",
            "requested_offering__offering",
        )
        .first()
    )
    recipient = User.objects.filter(uuid=recipient_uuid).first()
    if contribution is None or recipient is None:
        return
    actor = User.all_objects.filter(uuid=actor_uuid).first()
    deliver_all(contribution_delegated_mails(contribution, event, recipient, actor))


def contribution_delegated_mails(contribution, event, recipient, actor):
    """The mail telling one person what happened to a slot handed on."""
    if not recipient.email or not recipient.notifications_enabled:
        return []
    instance = contribution.proposal_step
    proposal = instance.proposal
    context = {
        "site_name": config.SITE_NAME,
        "event": event,
        "recipient_name": recipient.full_name or recipient.username,
        "actor_name": (actor.full_name or actor.username) if actor else "",
        "step_name": instance.call_step.name,
        "offering_name": (
            requested_offering.offering.name
            if (requested_offering := contribution.requested_offering) is not None
            else ""
        ),
        "proposal_name": proposal.name,
        "call_name": proposal.round.call.name,
        "due_at": (
            contribution.due_at.strftime("%Y-%m-%d") if contribution.due_at else ""
        ),
        # A slot of an offering is that offering's team's work: its view.
        "proposal_url": (
            offering_team_proposal_url(
                proposal.uuid, requested_offering.offering.customer.uuid
            )
            if requested_offering is not None
            and requested_offering.offering.customer_id
            else core_utils.format_homeport_link(
                "proposals/{proposal_uuid}/", proposal_uuid=proposal.uuid.hex
            )
        ),
    }
    return [
        Mail(
            "contribution_delegated",
            context,
            [recipient.email],
            audience="delegation",
            audience_detail=event,
            call=proposal.round.call,
            log={
                "proposal": proposal,
                "template": "contribution_delegated",
                "trigger": f"contribution_{event}",
                "audience": "contributor",
                "step_instance": instance,
            },
        )
    ]


@shared_task(name="waldur_mastermind.proposal.send_workflow_step_deadline_reminders")
def send_workflow_step_deadline_reminders():
    """Fire ``deadline_approaching`` rules for active steps whose lead time is today.

    A reminder is sent when the deadline falls ``days_before`` calendar days
    after today (``deadline.date() - now.date()``) and is recorded in the
    instance ledger so the daily beat cannot repeat it. Steps
    already past their deadline are left to ``mark_expired_workflow_steps``.
    """
    now = timezone.now()
    sent = 0
    # A round step's people are reminded once for the round, not per
    # proposal (send_round_report_reminders).
    instances = (
        proposal_models.ProposalWorkflowStepInstance.objects.filter(
            status=WorkflowStepInstanceStatuses.ACTIVE, deadline__gt=now
        )
        .exclude(call_step__round_scoped=True)
        .select_related("proposal__round__call__manager__customer")
    )
    for instance in instances:
        if instance.deadline is None:
            continue
        days_left = (instance.deadline.date() - now.date()).days
        rules = [
            rule
            for rule in notification_rules.enabled_rules(
                instance, NotificationRuleTriggers.DEADLINE_APPROACHING
            )
            if rule.days_before == days_left
        ]
        if not rules:
            continue
        key = notification_rules.ledger_key(
            NotificationRuleTriggers.DEADLINE_APPROACHING, days_left
        )
        with transaction.atomic():
            locked = (
                proposal_models.ProposalWorkflowStepInstance.objects.select_for_update()
                .filter(pk=instance.pk, status=WorkflowStepInstanceStatuses.ACTIVE)
                .first()
            )
            if locked is None or key in locked.sent_notifications:
                continue
            locked.sent_notifications = [*locked.sent_notifications, key]
            locked.save(update_fields=["sent_notifications"])
        try:
            _send_step_event(
                instance,
                NotificationRuleTriggers.DEADLINE_APPROACHING,
                rules,
                days_before=days_left,
            )
        except Exception:
            logger.exception(
                "Failed to send deadline reminder for workflow step instance %s",
                instance.uuid,
            )
            continue
        sent += 1
    if sent:
        logger.info("Sent %d workflow step deadline reminder(s)", sent)
    return sent


@shared_task(name="waldur_mastermind.proposal.lapse_change_requests")
def lapse_change_requests():
    """Close the requests for changes to an applicant past their due date.

    Each step goes back to its actor with its full duration, and they are
    told; nothing is decided for them. One transaction per request, so one
    failure does not hold up the rest.
    """
    overdue = list(
        change_requests.open_requests()
        .filter(contribution__isnull=True, due_at__lt=timezone.now())
        .values_list("id", "proposal_id", "step_instance_id")
    )
    lapsed = 0
    for request_id, proposal_id, instance_id in overdue:
        try:
            with transaction.atomic():
                # The order the step actions take: proposal, step, request.
                proposal_models.Proposal.objects.select_for_update().get(pk=proposal_id)
                instance = (
                    proposal_models.ProposalWorkflowStepInstance.objects.select_for_update()
                    .select_related("call_step")
                    .get(pk=instance_id)
                )
                change_request = (
                    change_requests.open_requests()
                    .select_for_update()
                    .filter(pk=request_id, due_at__lt=timezone.now())
                    .first()
                )
                if change_request is None:
                    continue
                change_request.step_instance = instance
                workflow_service.lapse_change_request(change_request)
        except Exception:
            logger.exception("Failed to lapse change request id=%s", request_id)
            continue
        lapsed += 1
    if lapsed:
        logger.info("Lapsed %d change request(s)", lapsed)
    return lapsed


def part_label(part):
    """How a part a request for changes unlocks reads in mail."""
    if part.startswith(ChangeRequestParts.FIELD_PREFIX):
        return part[len(ChangeRequestParts.FIELD_PREFIX) :].replace("_", " ")
    return {
        ChangeRequestParts.RUN_FORM: "form answers",
        ChangeRequestParts.RUN_REQUEST: "requested end date and resources",
    }.get(part, part)


def _change_request_audiences(change_request, event):
    """``[(users, is_applicant, evaluators, audience)]`` told of ``event``.

    The applicant team of a request for changes; the step's actor -- the
    call managers, and its completers -- of the changes submitted or
    lapsed; the contributor of a reopened contribution, the person it is
    handed to, and for a per-offering one its offering's managers. Nobody on the applicant team
    hears of an evaluation's work.
    """
    proposal = change_request.proposal
    instance = change_request.step_instance
    team_ids = proposal_roles.applicant_team_user_ids(proposal)
    if event == NotificationRuleTriggers.CHANGES_REQUESTED:
        return [
            (notification_rules.applicant_users(proposal), True, False, "applicant")
        ]
    if event == NotificationRuleTriggers.CONTRIBUTION_REOPENED:
        contribution = change_request.contribution
        ids = set()
        # The person the slot is handed to revises it; one who wrote it but
        # may no longer hold it is not asked to.
        delegate_id = contributions.effective_delegate_id(contribution)
        if delegate_id is not None:
            ids.add(delegate_id)
        if contribution.contributor_id and not (
            delegate_id is None
            and contribution.contributor_id == contribution.delegate_id
        ):
            ids.add(contribution.contributor_id)
        if contribution.requested_offering_id:
            ids |= set(
                proposal_permissions.offering_manager_ids(
                    [contribution.requested_offering.offering_id]
                )
            )
        return [
            (
                User.objects.filter(id__in=ids - set(team_ids)),
                False,
                True,
                "contributor",
            )
        ]
    managers = proposal.round.call.call_managers.exclude(id__in=team_ids)
    completers = (
        notification_rules.responsible_users(instance.call_step, proposal)
        .exclude(id__in=team_ids)
        .exclude(id__in=managers.values("id"))
    )
    return [
        (managers, False, False, "call_managers"),
        (completers, False, True, "responsible_role"),
    ]


@shared_task(name="waldur_mastermind.proposal.notify_changes_submitted")
def notify_changes_submitted(change_request_uuids):
    """Tell the actors of the steps whose requests for changes one
    submission answered: one mail per recipient, naming every request that
    concerns them -- a call manager both steps, a step's completer its own."""
    closed = list(
        proposal_models.ProposalChangeRequest.objects.select_related(
            "step_instance__call_step",
            "proposal__round__call__manager__customer",
        )
        .filter(uuid__in=change_request_uuids, contribution__isnull=True)
        .order_by("created", "id")
    )
    deliver_all(changes_submitted_mails(closed))


def changes_submitted_mails(closed):
    """The mails ``notify_changes_submitted`` sends for the requests one
    submission closed (``closed``, earliest first)."""
    if not closed:
        return []
    # email -> (requests concerning them, whether only as an evaluator)
    recipients = {}
    # email -> the account it was addressed through
    user_ids = {}
    for change_request in closed:
        for users, _is_applicant, evaluators, _audience in _change_request_audiences(
            change_request, NotificationRuleTriggers.CHANGES_SUBMITTED
        ):
            addressed = (
                users.filter(is_active=True)
                .exclude(email="")
                .exclude(notifications_enabled=False)
                .values_list("id", "email")
            )
            for user_id, email in addressed:
                requests, only_evaluator = recipients.get(email, ([], True))
                if change_request not in requests:
                    requests.append(change_request)
                recipients[email] = (requests, only_evaluator and evaluators)
                user_ids.setdefault(email, user_id)
    # One logged audience per (first request, as evaluators).
    logs = {}
    mails = []
    for email, (requests, evaluators) in sorted(recipients.items()):
        first = requests[0].step_instance
        # The offering managers completing a step open it as their team.
        provider = None
        if (
            evaluators
            and first.call_step.completer == ResponsibleRoles.OFFERING_MANAGER
        ):
            user = User.objects.filter(id=user_ids.get(email)).first()
            if user is not None:
                provider = contributions.offering_team_provider_uuid(
                    user, first.proposal
                )
        context = _step_event_context(
            first,
            NotificationRuleTriggers.CHANGES_SUBMITTED,
            False,
            evaluators=evaluators,
            provider_uuid=provider,
        )
        names = [
            request.step_instance.call_step.name or request.step_instance.step
            for request in requests
        ]
        context["step_name"] = ", ".join(names)
        context["submitted_requests"] = [
            {
                "step_name": name,
                "unlocked_parts": [part_label(part) for part in request.unlocked_parts],
            }
            for name, request in zip(names, requests)
        ]
        audience = _change_request_audience_name(
            NotificationRuleTriggers.CHANGES_SUBMITTED, False, evaluators
        )
        log = logs.setdefault(
            (requests[0].pk, evaluators),
            {
                "proposal": requests[0].proposal,
                "template": "workflow_step_event",
                "trigger": NotificationRuleTriggers.CHANGES_SUBMITTED,
                "audience": audience,
                "step_instance": requests[0].step_instance,
            },
        )
        mails.append(
            Mail(
                "workflow_step_event",
                context,
                [email],
                audience=audience,
                log=log,
                call=requests[0].proposal.round.call,
            )
        )
    return mails


def _change_request_audience_name(event, is_applicant, evaluators):
    """The role ``_change_request_audiences`` addressed, for the event log."""
    if is_applicant:
        return NotificationRuleRecipients.APPLICANT
    if event == NotificationRuleTriggers.CONTRIBUTION_REOPENED:
        return "contributor"
    if evaluators:
        return NotificationRuleRecipients.RESPONSIBLE_ROLE
    return NotificationRuleRecipients.CALL_MANAGERS


@shared_task(name="waldur_mastermind.proposal.notify_change_request")
def notify_change_request(change_request_uuid, event):
    """Tell whom it concerns of a request for changes: made, submitted,
    lapsed, or a contribution reopened (``event``)."""
    change_request = (
        proposal_models.ProposalChangeRequest.objects.select_related(
            "step_instance__call_step",
            "proposal__round__call__manager__customer",
            "contribution__requested_offering",
        )
        .filter(uuid=change_request_uuid)
        .first()
    )
    if change_request is None:
        return
    deliver_all(change_request_mails(change_request, event))


def change_request_mails(change_request, event):
    """The mails telling whom it concerns of a request for changes."""
    mails = []
    instance = change_request.step_instance
    for users, is_applicant, evaluators, audience in _change_request_audiences(
        change_request, event
    ):
        emails = sorted(
            set(
                users.filter(is_active=True)
                .exclude(email="")
                .exclude(notifications_enabled=False)
                .values_list("email", flat=True)
            )
        )
        if not emails:
            continue
        reopened = change_request.contribution
        offering = (
            reopened.requested_offering.offering
            if event == NotificationRuleTriggers.CONTRIBUTION_REOPENED
            and reopened is not None
            and reopened.requested_offering_id
            and reopened.requested_offering.offering.customer_id
            else None
        )
        # A reopened slot of an offering links its team's view of the
        # proposal, for those still holding a role on the offering; anyone
        # else -- a contributor who has since left it -- the plain page.
        groups = {None: emails}
        if offering is not None:
            team_emails = set(
                users.filter(
                    email__in=emails,
                    id__in=contributions.offering_role_holder_ids(offering.pk),
                ).values_list("email", flat=True)
            )
            groups = {
                offering.customer.uuid: sorted(team_emails),
                None: [email for email in emails if email not in team_emails],
            }
        for provider_uuid, group in groups.items():
            if not group:
                continue
            context = _step_event_context(
                instance,
                event,
                is_applicant,
                evaluators=evaluators,
                provider_uuid=provider_uuid,
            )
            if event in (
                NotificationRuleTriggers.CHANGES_REQUESTED,
                NotificationRuleTriggers.CONTRIBUTION_REOPENED,
            ):
                context["message"] = change_request.message
            if event == NotificationRuleTriggers.CHANGES_REQUESTED:
                context["unlocked_parts"] = [
                    part_label(part) for part in change_request.unlocked_parts
                ]
                context["due_at"] = change_request.due_at
            mails.append(
                Mail(
                    "workflow_step_event",
                    context,
                    group,
                    audience=audience,
                    single_message=is_applicant,
                    call=change_request.proposal.round.call,
                    cc=notification_rules.applicant_cc_emails(
                        change_request.proposal, group
                    )
                    if is_applicant
                    else [],
                    log={
                        "proposal": change_request.proposal,
                        "template": "workflow_step_event",
                        "trigger": event,
                        "audience": _change_request_audience_name(
                            event, is_applicant, evaluators
                        ),
                        "step_instance": instance,
                    },
                )
            )
    return mails


# -- the provider side of calls --------------------------------------------------


def _offering_manager_emails(offering):
    return list(
        dict.fromkeys(
            user.email
            for user in get_users(offering, role_name=RoleEnum.OFFERING_MANAGER)
            if user.email and user.notifications_enabled and user.is_active
        )
    )


def _provider_calls_url(offering):
    provider = offering.customer
    return core_utils.format_homeport_link(
        "providers/{provider_uuid}/calls/",
        provider_uuid=provider.uuid.hex if provider else "",
    )


@shared_task(name="waldur_mastermind.proposal.notify_offering_teams_on_round_start")
def notify_offering_teams_on_round_start():
    """Tell the managers of each offering a call takes in when one of its
    rounds opens today."""
    today = timezone.now().date()
    rounds = proposal_models.Round.objects.filter(
        call__state=CallStates.ACTIVE, start_time__date=today
    ).select_related("call")
    for call_round in rounds:
        steps = ", ".join(s.name for s in provider_calls.team_steps(call_round.call))
        for requested_offering in proposal_models.RequestedOffering.objects.filter(
            call=call_round.call, state=RequestedOfferingStates.ACCEPTED
        ).select_related("offering__customer"):
            offering = requested_offering.offering
            recipients = _offering_manager_emails(offering)
            if not recipients:
                continue
            core_utils.broadcast_mail(
                "proposal",
                "offering_round_opened",
                {
                    "site_name": config.SITE_NAME,
                    "offering_name": offering.name,
                    "call_name": call_round.call.name,
                    "round_name": call_round.name,
                    "cutoff": call_round.cutoff_time or proposal_models.NO_CUTOFF_TEXT,
                    "team_steps": steps,
                    "calls_url": _provider_calls_url(offering),
                },
                recipients,
            )


def _slot_holder(slot) -> str:
    if slot["delegate_name"]:
        return (
            f"handed to {slot['delegate_name']}"
            if slot["delegate_accepted"]
            else f"handed to {slot['delegate_name']}, not yet accepted"
        )
    if slot["stand_in"]:
        return "no offering managers -- the call's managers stand in"
    return "for the offering's managers"


@shared_task(name="waldur_mastermind.proposal.send_offering_team_digests")
def send_offering_team_digests():
    """A daily digest of the open work of each person's offering teams, to
    the offering managers and to those slots are handed to, when something
    opened since yesterday or fell overdue."""
    since = timezone.now() - timedelta(days=1)
    by_user: dict = {}
    for requested_offering in (
        proposal_models.RequestedOffering.objects.filter(
            state=RequestedOfferingStates.ACCEPTED,
            step_contributions__status__in=StepContributionStatuses.OPEN,
            step_contributions__proposal_step__status=WorkflowStepInstanceStatuses.ACTIVE,
        )
        .distinct()
        .select_related("offering__customer", "call")
    ):
        offering = requested_offering.offering
        slots = provider_calls.open_team_slots(requested_offering)
        if not slots:
            continue
        created = dict(
            proposal_models.StepContribution.objects.filter(
                uuid__in=[slot["uuid"] for slot in slots]
            ).values_list("uuid", "created")
        )
        managers = [
            user
            for user in get_users(offering, role_name=RoleEnum.OFFERING_MANAGER)
            if user.is_active
        ]
        for slot in slots:
            row = {
                **slot,
                "holder": _slot_holder(slot),
                "due_at": slot["due_at"].strftime("%Y-%m-%d") if slot["due_at"] else "",
                "is_new": created.get(slot["uuid"]) and created[slot["uuid"]] >= since,
                "proposal_url": offering_team_proposal_url(
                    slot["proposal_uuid"], offering.customer.uuid
                )
                if offering.customer
                else core_utils.format_homeport_link(
                    "proposals/{proposal_uuid}/",
                    proposal_uuid=slot["proposal_uuid"].hex,
                ),
                "calls_url": _provider_calls_url(offering),
            }
            people = list(managers)
            if slot["delegate_uuid"]:
                people += list(User.objects.filter(uuid=slot["delegate_uuid"]))
            for user in people:
                by_user.setdefault(user.pk, (user, []))[1].append(row)
    for user, rows in by_user.values():
        if not user.email or not user.notifications_enabled:
            continue
        new_count = sum(1 for row in rows if row["is_new"])
        overdue_count = sum(1 for row in rows if row["overdue"])
        if not new_count and not overdue_count:
            continue
        core_utils.broadcast_mail(
            "proposal",
            "offering_team_digest",
            {
                "site_name": config.SITE_NAME,
                "recipient_name": user.full_name or user.username,
                "slots": rows,
                "new_count": new_count,
                "overdue_count": overdue_count,
                "calls_url": rows[0]["calls_url"],
            },
            [user.email],
        )


@shared_task(name="waldur_mastermind.proposal.notify_call_offering_terms_changed")
def notify_call_offering_terms_changed(requested_offering_uuid):
    """Tell the managers of an accepted offering that the call changed what
    it asks of it."""
    requested_offering = (
        proposal_models.RequestedOffering.objects.filter(
            uuid=requested_offering_uuid, state=RequestedOfferingStates.ACCEPTED
        )
        .select_related("offering__customer", "call__manager__customer")
        .first()
    )
    if requested_offering is None:
        return
    offering = requested_offering.offering
    recipients = _offering_manager_emails(offering)
    if not recipients:
        return
    rules = [
        {
            "component_type": rule.component_type,
            "min_per_proposal": rule.min_per_proposal,
            "max_per_proposal": rule.max_per_proposal,
            "default_amount": rule.default_amount,
        }
        for rule in proposal_models.CallOfferingComponent.objects.filter(
            requested_offering=requested_offering
        )
    ]
    pools = [
        {
            "component_type": pool.offering_component.component_type,
            "round_name": pool.round.name if pool.round is not None else "",
            "offered_total": pool.offered_total,
        }
        for pool in proposal_models.CallResourcePool.objects.filter(
            offering_component__requested_offering=requested_offering
        ).select_related("offering_component", "round")
    ]
    core_utils.broadcast_mail(
        "proposal",
        "call_offering_terms_changed",
        {
            "site_name": config.SITE_NAME,
            "offering_name": offering.name,
            "call_name": requested_offering.call.name,
            "changed_by": requested_offering.call.manager.customer.name,
            "rules": rules,
            "pools": pools,
            "calls_url": _provider_calls_url(offering),
        },
        recipients,
    )


@shared_task(name="waldur_mastermind.proposal.notify_award_provision_failed")
def notify_award_provision_failed(awarded_resource_uuid):
    item = (
        proposal_models.AwardedResource.objects.filter(uuid=awarded_resource_uuid)
        .select_related("proposal__round__call", "requested_offering__offering")
        .first()
    )
    if item is None or not item.provision_error:
        return
    call = item.proposal.round.call
    recipients = list(
        dict.fromkeys(
            e for e in call.call_managers.values_list("email", flat=True) if e
        )
    )
    if not recipients:
        return
    core_utils.broadcast_mail(
        "proposal",
        "award_provision_failed",
        {
            "site_name": config.SITE_NAME,
            "call_name": call.name,
            "proposal_name": item.proposal.name,
            "offering_name": item.requested_offering.offering.name,
            "error": item.provision_error,
            "proposal_url": core_utils.format_homeport_link(
                "proposals/{proposal_uuid}/", proposal_uuid=item.proposal.uuid.hex
            ),
        },
        recipients,
    )
