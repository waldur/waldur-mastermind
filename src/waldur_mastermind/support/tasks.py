import copy
import logging
import os
import uuid
from datetime import timedelta
from smtplib import SMTPException

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core import signing
from django.core.files.base import ContentFile
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F, Q
from django.template import Context, Template
from django.template.loader import get_template
from django.utils import timezone
from markdownify import markdownify

from waldur_core.configuration import config
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.utils import broadcast_mail, text2html

from . import backend, models
from .utils import (
    format_issue_subject,
    get_feedback_link,
    get_issue_thread_headers,
)

logger = logging.getLogger(__name__)


@shared_task(name="waldur_mastermind.support.pull_support_users")
def pull_support_users():
    """Pull support users from the active support backend."""
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    backend.get_active_backend().pull_support_users()


@shared_task(name="waldur_mastermind.support.pull_priorities")
def pull_priorities():
    """Pull priority levels from the active support backend."""
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    backend.get_active_backend().pull_priorities()


@shared_task(name="waldur_mastermind.support.create_issue")
def create_issue(serialized_issue):
    issue = core_utils.deserialize_instance(serialized_issue)
    try:
        backend.get_active_backend().create_issue(issue)
    except Exception as e:
        issue.error_message = str(e)
        issue.save(update_fields=["error_message"])
    else:
        issue.error_message = ""
        issue.save(update_fields=["error_message"])


@shared_task(name="waldur_mastermind.support.create_confirmation_comment")
def create_confirmation_comment(serialized_issue, comment_tmpl=""):
    issue = core_utils.deserialize_instance(serialized_issue)
    try:
        backend.get_active_backend().create_confirmation_comment(issue, comment_tmpl)
    except Exception as e:
        issue.error_message = str(e)
        issue.save(update_fields=["error_message"])
    else:
        issue.error_message = ""
        issue.save(update_fields=["error_message"])


@shared_task(name="waldur_mastermind.support.send_issue_updated_notification")
def send_issue_updated_notification(serialized_issue, changed):
    issue = core_utils.deserialize_instance(serialized_issue)
    extra_context = {
        "changed": changed,
        "format_description": issue.description,
        "format_old_description": changed.get("description", ""),
    }

    _send_issue_notification(
        issue=issue,
        template="issue_updated",
        extra_context=extra_context,
        notification_key="support.notification_issue_updated",
    )


@shared_task(name="waldur_mastermind.support.send_comment_added_notification")
def send_comment_added_notification(serialized_comment):
    comment = core_utils.deserialize_instance(serialized_comment)
    is_system_comment = False

    if (
        config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE == backend.SupportBackendType.SMAX
        and comment.author.name == config.SMAX_LOGIN
    ):
        is_system_comment = True

    if comment_reaches_helpdesk(comment):
        notify_helpdesk_new_comment(comment)

    if not comment_reaches_caller(comment):
        # The caller wrote it, or the ticket has no caller to tell. Letting the
        # latter fall through would queue a send that can only log a warning
        # and give up.
        return

    _send_issue_notification(
        issue=comment.issue,
        template="comment_added",
        extra_context={
            "comment": comment,
            "format_description": comment.description,
            "is_system_comment": is_system_comment,
        },
        notification_key="support.notification_comment_added",
    )


def notify_helpdesk_new_comment(comment):
    """Tell whoever works the ticket that someone outside the helpdesk has
    commented."""
    _notify_helpdesk_about_comment(
        comment,
        "notification_comment_added_staff",
        "support.notification_comment_added",
    )


@shared_task(name="waldur_mastermind.support.notify_helpdesk_comment_updated")
def notify_helpdesk_comment_updated(serialized_comment, old_description):
    """Tell whoever works the ticket that someone outside the helpdesk has
    edited a comment.

    They may already have acted on the original, so the old text goes along
    with the new one.
    """
    comment = core_utils.deserialize_instance(serialized_comment)
    _notify_helpdesk_about_comment(
        comment,
        "notification_comment_updated_staff",
        "support.notification_comment_updated",
        {"old_description": old_description},
    )


def _notify_helpdesk_about_comment(
    comment, template, caller_notification_key, extra_context=None
):
    issue = comment.issue

    # `broadcast_mail` consults only the Notification row, where `_send_email`
    # checks this as well. Without it a deployment with support switched off
    # would still mail its whole staff roster.
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    if config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE != backend.SupportBackendType.BASIC:
        # Atlassian, Zammad and SMAX show the comment to their own agents.
        return

    if issue.provider_helpdesk_id:
        # A ticket routed to a provider is announced by
        # `notify_provider_customer_comment` instead.
        return

    # A caller who also works the helpdesk must not be told twice, but nor left
    # out when the notification addressed to them as the caller is switched off.
    caller_is_told = (
        comment_reaches_caller(comment)
        and core_models.Notification.objects.filter(
            key=caller_notification_key, enabled=True
        ).exists()
    )
    recipients = get_helpdesk_comment_recipients(comment, caller_is_told)
    if not recipients:
        logger.info("No helpdesk recipient for a comment on issue %s.", issue.key)
        return

    try:
        broadcast_mail(
            "support",
            template,
            {
                "issue": issue,
                "comment": comment,
                "issue_url": core_utils.format_homeport_link(
                    "support/issue/{uuid}/", uuid=issue.uuid
                ),
                **(extra_context or {}),
            },
            recipients,
            headers=get_issue_thread_headers(issue.uuid),
        )
    except Exception:
        logger.exception(
            "Failed to notify the helpdesk about a comment on issue %s", issue.key
        )


@shared_task(name="waldur_mastermind.support.send_comment_updated_notification")
def send_comment_updated_notification(serialized_comment, old_description):
    comment = core_utils.deserialize_instance(serialized_comment)

    _send_issue_notification(
        issue=comment.issue,
        template="comment_updated",
        extra_context={
            "comment": comment,
            "format_description": comment.description,
            "format_old_description": old_description,
        },
        notification_key="support.notification_comment_updated",
    )


def _send_email(
    issue: models.Issue,
    html_template,
    text_template,
    subject_template,
    receiver: core_models.User = None,
    extra_context=None,
    notification_key=None,
):
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    # Since support email notifications are sent out through this function rather that broadcast_email()
    # we need to check if the notification is enabled here. For that we introduce a new parameter notification_key
    # which is used to identify the notification.
    if notification_key:
        try:
            notification = core_models.Notification.objects.get(key=notification_key)
            if not notification.enabled:
                message = (
                    "Notification %s is disabled. Please enable it to send notifications."
                    % notification_key
                )
                logger.info(message)
                return
        except core_models.Notification.DoesNotExist:
            return

    if not receiver:
        receiver = issue.caller
        if receiver is None:
            logger.warning(
                f"Issue has no connected caller, cannot send an update for issue {issue.uuid}."
            )
            return

    context = {
        "issue_url": core_utils.format_homeport_link(
            "support/issue/{uuid}/", uuid=issue.uuid
        ),
        "site_name": config.SITE_NAME,
        "issue": issue,
    }

    if extra_context:
        context.update(extra_context)

    html_context = copy.deepcopy(context)
    text_context = copy.deepcopy(context)

    if backend.get_active_backend().message_format == backend.SupportedFormat.HTML:
        html_format = True
    else:
        html_format = False

    for k in list(text_context):
        if k.startswith("format_"):
            if html_format:
                text_context[k.replace("format_", "")] = markdownify(text_context[k])
            else:
                text_context[k.replace("format_", "")] = text_context[k]

    for k in list(html_context):
        if k.startswith("format_"):
            if not html_format:
                html_context[k.replace("format_", "")] = text2html(html_context[k])
            else:
                html_context[k.replace("format_", "")] = html_context[k]

    html_message = html_template.render(Context(html_context))
    text_message = text_template.render(Context(text_context, autoescape=False))
    subject = format_issue_subject(
        subject_template.render(Context(context, autoescape=False)).strip(), issue
    )

    logger.info("About to send an issue update notification to %s" % receiver.email)

    try:
        core_utils.send_mail(
            subject,
            text_message,
            [receiver.email],
            html_message=html_message,
            headers=get_issue_thread_headers(issue.uuid),
        )
    except SMTPException as e:
        error_message = str(e)
        message = f"Failed to notify a user about an issue update. Issue uuid: {issue.uuid.hex}. Error: {error_message}"
        logger.warning(message)


def _send_issue_notification(issue: models.Issue, template, *args, **kwargs):
    try:
        notification_template = models.TemplateStatusNotification.objects.get(
            status=issue.status
        )
        html_template = Template(notification_template.html)
        text_template = Template(notification_template.text)
        subject_template = Template(notification_template.subject)
    except models.TemplateStatusNotification.DoesNotExist:
        html_template = get_template(
            "support/notification_%s_message.html" % template
        ).template
        text_template = get_template(
            "support/notification_%s_message.txt" % template
        ).template
        subject_template = get_template(
            "support/notification_%s_subject.txt" % template
        ).template
    _send_email(issue, html_template, text_template, subject_template, *args, **kwargs)


def _send_issue_feedback(issue, template, *args, **kwargs):
    html_template = get_template(
        "support/notification_%s_message.html" % template
    ).template
    text_template = get_template(
        "support/notification_%s_message.txt" % template
    ).template
    subject_template = get_template(
        "support/notification_%s_subject.txt" % template
    ).template
    _send_email(issue, html_template, text_template, subject_template, *args, **kwargs)


@shared_task(name="waldur_mastermind.support.send_issue_feedback_notification")
def send_issue_feedback_notification(serialized_issue):
    issue = core_utils.deserialize_instance(serialized_issue)
    signer = signing.TimestampSigner()
    token = signer.sign(issue.uuid.hex)
    extra_context = {
        "feedback_link": get_feedback_link(token),
        "feedback_links": [
            {
                "label": str(index),
                "link": get_feedback_link(token, str(index)),
            }
            for index in range(1, 11)
        ],
    }
    _send_issue_feedback(
        issue=issue,
        template="issue_feedback",
        extra_context=extra_context,
        notification_key="support.notification_issue_feedback",
    )


@shared_task(name="waldur_mastermind.support.sync_feedback")
def sync_feedback(serialized_feedback):
    feedback = core_utils.deserialize_instance(serialized_feedback)
    feedback.state = feedback.States.CREATING
    feedback.save()
    backend.get_active_backend().create_feedback(feedback)


@shared_task(name="waldur_mastermind.support.sync_request_types")
def sync_request_types():
    """Synchronize request types from the active support backend."""
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    active_backend = backend.get_active_backend()

    if not hasattr(active_backend, "pull_request_types"):
        return

    backend.get_active_backend().pull_request_types()


@shared_task(name="waldur_mastermind.support.sync_issues")
def sync_issues():
    backend.get_active_backend().sync_issues()


def resolve_routing_offering(issue):
    """Offering that determines provider routing for an issue.

    Prefers the offering linked directly on the issue (a ticket opened about an
    offering, possibly with no resource); otherwise derives it from the attached
    resource's marketplace offering.
    """
    from waldur_mastermind.marketplace import models as marketplace_models

    if issue.offering_id:
        return issue.offering

    resource = issue.resource
    if not resource:
        return None

    if isinstance(resource, marketplace_models.Resource):
        return resource.offering

    marketplace_resource = marketplace_models.Resource.objects.filter(
        scope=resource
    ).first()
    return marketplace_resource.offering if marketplace_resource else None


@shared_task(
    name="waldur_mastermind.support.route_issue_to_provider",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def route_issue_to_provider(self, issue_id):
    """Route an issue to the appropriate provider helpdesk by creating a child issue."""
    try:
        issue = models.Issue.objects.get(id=issue_id)
    except models.Issue.DoesNotExist:
        logger.error("Issue %s not found for routing.", issue_id)
        return

    # Skip if already routed
    if issue.child_issues.exists():
        logger.info("Issue %s already routed, skipping.", issue.key)
        return

    # Resolve: Issue -> Offering -> ServiceProvider -> ProviderHelpdesk.
    # The offering is the routing determinant; it comes either from the issue
    # directly (e.g. a ticket opened from an offering, no resource) or from the
    # attached resource's offering.
    from waldur_mastermind.marketplace import models as marketplace_models

    resource = issue.resource
    offering = resolve_routing_offering(issue)
    if not offering or not offering.customer:
        logger.info(
            "No routable offering for issue %s, stays with operator.", issue.key
        )
        return

    try:
        service_provider = marketplace_models.ServiceProvider.objects.get(
            customer=offering.customer
        )
    except marketplace_models.ServiceProvider.DoesNotExist:
        logger.info("No service provider for offering customer of issue %s.", issue.key)
        return

    try:
        provider_helpdesk = models.ProviderHelpdesk.objects.get(
            service_provider=service_provider, is_active=True
        )
    except models.ProviderHelpdesk.DoesNotExist:
        logger.info("No active provider helpdesk for issue %s.", issue.key)
        return

    try:
        child_issue = create_provider_child_issue(issue, provider_helpdesk, resource)
        logger.info(
            "Successfully routed issue %s to provider %s (child: %s).",
            issue.key,
            service_provider,
            child_issue.key,
        )
        notify_provider_new_ticket.delay(child_issue.id)
    except Exception as exc:
        provider_helpdesk.failed_routing_count += 1
        provider_helpdesk.save(update_fields=["failed_routing_count"])
        logger.exception("Failed to route issue %s to provider.", issue.key)
        raise self.retry(exc=exc)


def create_provider_child_issue(issue, provider_helpdesk, resource=None):
    """Create a child issue routed to the given provider helpdesk and push it to the provider backend.

    Shared by automatic routing (route_issue_to_provider) and manual routing
    (IssueViewSet.route_to_provider). Raises on backend failure; the caller decides
    how to handle it (retry vs. surfacing an error). The parent's attachments are
    copied by ``forward_parent_attachments`` once the child is committed.
    """
    from .utils import build_provider_context

    context = build_provider_context(issue, resource)
    enriched_description = (
        f"Routed from operator ticket {issue.key}\n\n"
        f"Customer: {context.get('customer_name', 'N/A')}\n"
        f"Project: {context.get('project_name', 'N/A')}\n"
        f"Resource: {context.get('resource_name', 'N/A')}\n\n"
        f"{issue.description}"
    )

    child_issue = models.Issue.objects.create(
        summary=issue.summary,
        description=enriched_description,
        type=issue.type,
        priority=issue.priority,
        status=issue.status,
        caller=issue.caller,
        customer=issue.customer,
        project=issue.project,
        parent_issue=issue,
        provider_helpdesk=provider_helpdesk,
    )

    # Call provider backend to create the issue
    from .backend import get_backend_for_provider

    provider_backend = get_backend_for_provider(provider_helpdesk)
    provider_backend.create_issue(child_issue)

    issue.append_processing_log(
        "routed_to_provider",
        {
            "child_issue_uuid": child_issue.uuid.hex,
            "provider": str(provider_helpdesk.service_provider),
        },
    )
    issue.save(update_fields=["processing_log"])

    # The customer's files follow once the child is committed, in a task that
    # can retry a desk that is down without holding the routing back.
    child_issue_id = child_issue.id
    transaction.on_commit(lambda: forward_parent_attachments.delay(child_issue_id))

    return child_issue


def attachment_is_forwardable(attachment) -> bool:
    """May the attachment travel to the other side of a routing?

    Not a copy, which has travelled already, nor a file from an internal note,
    which stays with whoever wrote it, as the note does. A row whose file has
    not been saved yet has nothing to copy; the save that adds the file
    forwards it.
    """
    return (
        not attachment.is_forwarded and attachment.is_public and bool(attachment.file)
    )


#: How many pushes of one copy before it is given up as ERRED.
MAX_PUSH_ATTEMPTS = 24
#: How many stuck copies one run of ``retry_attachment_forwarding`` pushes.
PUSH_RETRY_BATCH = 100
#: How long a copy left waiting rests before the periodic task pushes it, so
#: as not to race the forwarding task's own retries.
PUSH_RETRY_PAUSE = timedelta(minutes=15)


def push_claim_timeout() -> timedelta:
    """How long a push may hold its claim on a copy.

    A claim older than this was left by a worker that died, and the copy may
    be pushed again. Twice the hard task time limit, so that no live push,
    which Celery kills by then, is taken over.
    """
    limit = getattr(settings, "CELERY_TASK_TIME_LIMIT", None) or 1800
    return timedelta(seconds=2 * limit)


def forward_attachment(attachment, issue, issue_backend):
    """Copy an attachment to the other side of a routing, once.

    ``issue`` is the parent or a child of the attachment's issue, and
    ``issue_backend`` the backend that owns it. The copy is pushed there, as
    an upload to that ticket would be.

    The copy is marked forwarded, so it never travels back, and records its
    original, so routing and forwarding racing each other make one copy, not
    two. It gets a stored file of its own, so deleting or erasing either
    attachment leaves the other readable. It is committed before it is pushed,
    and waits in ``CREATION_SCHEDULED`` until its desk has it (see
    ``push_copy``).

    Returns the copy, or None when the attachment is not to be forwarded.
    Raises when the desk does not take the file, leaving the copy for the next
    attempt.
    """
    if not attachment_is_forwardable(attachment):
        return None
    forwarded = _commit_copy(attachment, issue)
    push_copy(forwarded, issue_backend)
    try:
        forwarded.refresh_from_db()
    except models.Attachment.DoesNotExist:
        return None
    return forwarded


def _commit_copy(attachment, issue):
    """The copy of ``attachment`` on ``issue``, committed: the one there is,
    or a new one waiting to be pushed."""
    existing = issue.attachments.filter(forwarded_from=attachment).first()
    if existing:
        return existing
    try:
        with transaction.atomic():
            with attachment.file.open("rb") as source:
                content = source.read()
            forwarded = models.Attachment(
                issue=issue,
                author=attachment.author,
                is_forwarded=True,
                forwarded_from=attachment,
                state=CoreStates.CREATION_SCHEDULED,
                # The provider's desk numbers attachments from a sequence of
                # its own; its name keeps those ids apart from everyone else's.
                backend_name=backend.attachment_backend_name_for(issue),
            )
            # A placeholder the desk replaces with its own id. One still in
            # place after the push means the desk took nothing.
            forwarded.backend_id = backend.build_backend_id(
                forwarded.uuid, PLACEHOLDER_MARKER
            )
            forwarded.file.save(
                os.path.basename(attachment.file.name),
                ContentFile(content),
                save=False,
            )
            forwarded.save()
    except IntegrityError:
        # Another task made the copy while this one was making it. The
        # unique constraint held this insert back until that one committed.
        existing = issue.attachments.filter(forwarded_from=attachment).first()
        if existing:
            return existing
        raise
    return forwarded


#: Marks the placeholder id of a copy not yet on its desk.
PLACEHOLDER_MARKER = "AF"


def _has_placeholder(attachment) -> bool:
    # Read off the id itself rather than rebuilt, so that a key prefix changed
    # since the copy was made does not make a placeholder look like a desk's.
    return (attachment.backend_id or "").endswith(
        f"-{PLACEHOLDER_MARKER}-{attachment.uuid.hex[:8].upper()}"
    )


def push_copy(forwarded, issue_backend):
    """Push a committed copy to the desk of its ticket, unless it is there.

    Three steps, so that no transaction is open while the desk is called: a
    slow desk then holds no database connection idle in a transaction, and a
    reroute deleting the ticket does not wait for it.

    1. Claim the copy (``CREATING``) in a short transaction. Only a copy
       waiting to be pushed, or one whose claim a dead worker left behind,
       can be claimed, so two tasks never push the same copy at once.
    2. Call the desk.
    3. Record the result in another short transaction.

    A desk that has replaced the placeholder id has taken the file, even when
    saving the id then fails: the copy is recorded as pushed, not sent again.
    Should recording fail too, a copy whose desk id reached the row is
    recognised as pushed on the next attempt. Raises when the desk did not
    take the file, after handing the copy back to wait for another attempt.
    """
    claimed = _claim_copy(forwarded)
    if claimed is None:
        return

    try:
        issue_backend.create_attachment(claimed)
    except Exception:
        if _has_placeholder(claimed):
            _release_copy(claimed)
            raise
        logger.exception(
            "The desk took attachment %s as %s, but saving that failed; "
            "recording it again.",
            claimed.uuid.hex,
            claimed.backend_id,
        )
    if _has_placeholder(claimed):
        _release_copy(claimed)
        raise backend.SupportBackendError(
            f"The desk did not take attachment {claimed.uuid.hex}."
        )
    _record_push(claimed)


def _claim_copy(forwarded):
    """Claim a copy for one push, or None if it is not to be pushed now."""
    now = timezone.now()
    with transaction.atomic():
        copy_row = (
            models.Attachment.objects.select_for_update()
            .filter(pk=forwarded.pk)
            .first()
        )
        if copy_row is None:
            # Gone with its ticket, which a reroute withdrew.
            return None
        if copy_row.state == CoreStates.CREATING:
            if copy_row.modified > now - push_claim_timeout():
                # Another worker is pushing it.
                return None
        elif copy_row.state != CoreStates.CREATION_SCHEDULED:
            # On its desk already, or given up.
            return None

        if not _has_placeholder(copy_row):
            # The desk took it on an earlier attempt and the backend saved its
            # id, but recording the push failed.
            models.Attachment.objects.filter(pk=copy_row.pk).update(
                state=CoreStates.OK, modified=now
            )
            return None
        if copy_row.push_attempts >= MAX_PUSH_ATTEMPTS:
            models.Attachment.objects.filter(pk=copy_row.pk).update(
                state=CoreStates.ERRED,
                modified=now,
                error_message=(
                    f"The desk did not take the file in {copy_row.push_attempts} "
                    "attempts."
                ),
            )
            return None

        models.Attachment.objects.filter(pk=copy_row.pk).update(
            state=CoreStates.CREATING,
            modified=now,
            push_attempts=F("push_attempts") + 1,
        )
    copy_row.state = CoreStates.CREATING
    copy_row.push_attempts += 1
    # The claim: the push records its result only while the row still holds
    # it, and the backend's own save leaves it alone (Attachment.save).
    copy_row.modified = now
    copy_row.pushing = True
    return copy_row


def _release_copy(claimed):
    """Hand a copy the desk did not take back to wait for another attempt."""
    try:
        models.Attachment.objects.filter(
            pk=claimed.pk, state=CoreStates.CREATING, modified=claimed.modified
        ).update(state=CoreStates.CREATION_SCHEDULED, modified=timezone.now())
    except DatabaseError:
        # The claim stays, and lapses after push_claim_timeout().
        logger.exception("Failed to release attachment %s.", claimed.uuid.hex)


def _record_push(claimed):
    """Record that the desk has the copy, under the id it gave it."""
    try:
        with transaction.atomic():
            recorded = models.Attachment.objects.filter(
                pk=claimed.pk, state=CoreStates.CREATING, modified=claimed.modified
            ).update(
                backend_id=claimed.backend_id,
                backend_name=claimed.backend_name,
                state=CoreStates.OK,
                modified=timezone.now(),
            )
        if not recorded:
            logger.info(
                "Attachment %s is gone with its ticket, or another push took it "
                "over; this push is not recorded.",
                claimed.uuid.hex,
            )
        return
    except DatabaseError as e:
        logger.exception(
            "The desk took attachment %s as %s, but recording it failed.",
            claimed.uuid.hex,
            claimed.backend_id,
        )
        error = e
    try:
        models.Attachment.objects.filter(
            pk=claimed.pk, state=CoreStates.CREATING, modified=claimed.modified
        ).update(
            state=CoreStates.ERRED,
            error_message=(
                f"The desk took the file as {claimed.backend_id}, but recording "
                f"that failed: {error}"
            ),
        )
    except DatabaseError:
        # Nothing can be written now. If the backend saved the desk's id, the
        # next attempt finds it there and records the push without sending
        # the file again.
        logger.exception(
            "Failed to record attachment %s as erred either.", claimed.uuid.hex
        )


def _note_not_forwarded(parent_issue, failed):
    """Note on the parent's processing log the files the other side is
    without, as ``(original, issue, copy, reason)``; the original or the copy
    may be None."""
    if not failed:
        return
    parent_issue.refresh_from_db(fields=["processing_log"])
    for original, issue, forwarded, reason in failed:
        parent_issue.append_processing_log(
            "attachment_not_forwarded",
            {
                "attachment_uuid": original.uuid.hex if original else None,
                "issue_uuid": issue.uuid.hex,
                "copy_uuid": forwarded.uuid.hex if forwarded else None,
                "reason": reason,
            },
        )
    parent_issue.save(update_fields=["processing_log"])


def _give_up(forwarded, reason):
    """Mark a copy on its way as ERRED for ``reason``, and note it once."""
    given_up = models.Attachment.objects.filter(
        models.forwarding_q(), pk=forwarded.pk
    ).update(
        state=CoreStates.ERRED,
        modified=timezone.now(),
        error_message=f"Not forwarded: {reason}.",
    )
    if given_up:
        issue = forwarded.issue
        _note_not_forwarded(
            issue.parent_issue or issue,
            [(forwarded.forwarded_from, issue, forwarded, reason)],
        )


def _reason_erred(forwarded) -> str:
    if forwarded.push_attempts >= MAX_PUSH_ATTEMPTS:
        return "given_up"
    return "not_recorded"


def _forward_attachments(task, parent_issue, pairs):
    """Forward each ``(attachment, issue)`` pair, retrying ``task`` while a
    desk refuses any of them.

    Every pair is tried before the task retries, and a retry only pushes the
    copies still waiting for their desk. Once the retries are spent, what is
    still waiting is logged and noted on the parent's processing log, as is a
    copy whose push could not be recorded, so staff can see what the other
    side is without. ``retry_attachment_forwarding`` takes over from there.
    """
    backends = {}
    waiting = []
    erred = []
    for attachment, issue in pairs:
        if backend.is_orphaned(issue):
            # Its helpdesk is gone: nothing more reaches its desk, and what
            # was on the way is given up.
            for forwarded in issue.attachments.filter(
                models.forwarding_q(), forwarded_from=attachment
            ):
                _give_up(forwarded, "helpdesk_deleted")
            continue
        if issue.attachments.filter(
            forwarded_from=attachment, state=CoreStates.ERRED
        ).exists():
            # Noted when it erred; it waits for a person.
            continue
        try:
            if issue.id not in backends:
                backends[issue.id] = backend.get_backend_for_issue(issue)
            forwarded = forward_attachment(attachment, issue, backends[issue.id])
        except Exception:
            logger.exception(
                "Failed to forward attachment %s to issue %s.",
                attachment.uuid.hex,
                issue.key,
            )
            waiting.append((attachment, issue))
        else:
            if forwarded is not None and forwarded.state == CoreStates.ERRED:
                erred.append((attachment, issue, forwarded, _reason_erred(forwarded)))

    if waiting and task.request.retries < task.max_retries:
        raise task.retry()
    _note_not_forwarded(
        parent_issue,
        [
            (
                attachment,
                issue,
                issue.attachments.filter(forwarded_from=attachment).first(),
                "not_taken",
            )
            for attachment, issue in waiting
        ]
        + erred,
    )


def reroute_issue_to_provider(issue, new_helpdesk):
    """Move an already-routed issue from its current provider helpdesk to a new one.

    Tears down the existing child issue(s) through the ORIGINAL provider's backend
    (a real delete for jira/zammad; a no-op for the basic backend, where the child
    row is the whole ticket), then creates a fresh child for ``new_helpdesk``.
    Returns ``(new_child_issue, withdrawn)``, where ``withdrawn`` holds one
    ``(old_helpdesk, child_uuid_hex, child_key)`` per torn-down child -- the
    child rows are gone by then, and the withdrawal notice needs their identity
    to reach the provider in the thread they have been reading. The caller runs
    this inside a transaction and dispatches notifications on commit.
    """
    from .backend import get_backend_for_provider

    # Create the new child FIRST. An already-deleted external ticket
    # (jira/zammad) cannot be restored by an ORM rollback, so tearing the old
    # one down before the new routing succeeds would risk losing it when
    # create_provider_child_issue fails. Creating first leaves the original
    # ticket intact on failure.
    new_child = create_provider_child_issue(issue, new_helpdesk, issue.resource)

    withdrawn = []
    for child in (
        issue.child_issues.select_related("provider_helpdesk")
        .exclude(id=new_child.id)
        .all()
    ):
        old_helpdesk = child.provider_helpdesk
        if old_helpdesk:
            withdrawn.append((old_helpdesk, child.uuid.hex, child.key))
            try:
                get_backend_for_provider(old_helpdesk).delete_issue(child)
            except Exception:
                # Best-effort teardown: a backend that cannot retract the ticket
                # (email/smax) leaves an external artifact; the withdrawal
                # notification tells the old provider to drop it.
                logger.exception(
                    "Failed to remove child issue %s from provider %s during "
                    "reroute; the external ticket may be orphaned.",
                    child.key,
                    old_helpdesk,
                )
        # The rows go with the child; their stored files would stay behind.
        # Each is the child's own, copies included, so nothing else loses it.
        for attachment in child.attachments.all():
            attachment.file.delete(save=False)
        child.delete()

    issue.append_processing_log(
        "rerouted_to_provider",
        {
            "child_issue_uuid": new_child.uuid.hex,
            "from": [str(h.service_provider) for h, _, _ in withdrawn],
            "to": str(new_helpdesk.service_provider),
        },
    )
    issue.save(update_fields=["processing_log"])

    return new_child, withdrawn


def get_helpdesk_personnel():
    """The people who work the operator's helpdesk, as users.

    `is_staff or is_support` is the predicate the support app uses everywhere
    for helpdesk personnel; `SupportUser` is not a usable roster on the built-in
    backend, where rows only appear incidentally when somebody comments or
    resolves. Honours each user's own notification opt-out.
    """
    return (
        core_models.User.objects.filter(is_active=True, notifications_enabled=True)
        .filter(Q(is_staff=True) | Q(is_support=True))
        .exclude(email="")
    )


def _comment_author_user(comment):
    """The user who wrote a comment, or None for one imported from a remote
    service desk."""
    return comment.author.user


def _issue_assignee_user(issue):
    return issue.assignee.user if issue.assignee_id else None


def comment_reaches_helpdesk(comment) -> bool:
    """Whether whoever works the ticket should hear about a comment.

    They hear about every comment the helpdesk did not write itself: the
    caller's, and those of anyone else on the customer's side who may comment,
    such as an organization owner or a project admin. Staff, support users and
    the ticket's assignee write for the helpdesk. A staff member who raised the
    ticket is its caller, so the rest of the desk hears from them too.
    """
    author = _comment_author_user(comment)
    if author is None:
        # Imported from a remote service desk, whose own agents have seen it.
        return False
    issue = comment.issue
    if author == issue.caller:
        return True
    return not (
        author.is_staff or author.is_support or author == _issue_assignee_user(issue)
    )


def comment_reaches_caller(comment) -> bool:
    """Whether the caller should hear about a comment: whenever someone else
    wrote it."""
    caller = comment.issue.caller
    return caller is not None and _comment_author_user(comment) != caller


def get_helpdesk_comment_recipients(comment, caller_is_told=False) -> list[str]:
    """Who on the helpdesk to tell about a comment from outside it.

    The assignee owns the ticket once it has one, so they alone are told;
    otherwise nobody in particular owns it yet and the whole helpdesk hears.
    The author is never on the list, and neither is the caller when
    `caller_is_told`, that is, when the notification addressed to them as the
    caller goes out. Both matter when a staff member raised the ticket.
    """
    issue = comment.issue
    author = _comment_author_user(comment)
    caller = issue.caller if caller_is_told else None

    assignee = _issue_assignee_user(issue)
    if (
        assignee is not None
        and assignee != author
        and assignee.is_active
        and assignee.notifications_enabled
        and assignee.email
    ):
        # An assignee who is also the caller has been told already, and the
        # ticket is theirs, so nobody else needs to be.
        return [] if assignee == caller else [assignee.email]

    # No reachable assignee: an agent who has left, or who switched
    # notifications off, must not swallow the reply. Fall back to the whole
    # helpdesk, the way the new-ticket notification does.

    personnel = get_helpdesk_personnel().exclude(
        pk__in=[user.pk for user in (author, caller) if user is not None]
    )
    return list(personnel.values_list("email", flat=True).distinct())


def get_helpdesk_personnel_emails() -> list[str]:
    """Addresses of the people who work the operator's helpdesk."""
    return list(get_helpdesk_personnel().values_list("email", flat=True).distinct())


@shared_task(name="waldur_mastermind.support.notify_staff_new_issue")
def notify_staff_new_issue(issue_id):
    """Tell helpdesk personnel that a support request has been created.

    Only the built-in service desk uses this: Atlassian, Zammad and SMAX notify
    their own agents, and a ticket routed to a provider helpdesk is announced by
    `notify_provider_new_ticket` instead.
    """
    try:
        issue = models.Issue.objects.select_related(
            "caller", "customer", "project"
        ).get(id=issue_id)
    except models.Issue.DoesNotExist:
        return

    recipients = get_helpdesk_personnel_emails()
    if not recipients:
        logger.info(
            "No staff or support user is available to notify about issue %s.",
            issue.key,
        )
        return

    try:
        broadcast_mail(
            "support",
            "notification_issue_created",
            {"issue": issue},
            recipients,
            headers=get_issue_thread_headers(issue.uuid),
        )
    except Exception:
        logger.exception(
            "Failed to send new issue notification for issue %s", issue.key
        )


@shared_task(name="waldur_mastermind.support.notify_provider_new_ticket")
def notify_provider_new_ticket(issue_id):
    """Notify provider about a new ticket routed to their helpdesk."""
    try:
        issue = models.Issue.objects.select_related("provider_helpdesk").get(
            id=issue_id
        )
    except models.Issue.DoesNotExist:
        return

    helpdesk = issue.provider_helpdesk
    if not helpdesk or not helpdesk.notify_on_new_ticket:
        return

    recipients = helpdesk.get_notification_emails()
    if recipients:
        try:
            broadcast_mail(
                "support",
                "provider_new_ticket",
                {"issue": issue, "provider_helpdesk": helpdesk},
                recipients,
                headers=get_issue_thread_headers(issue.uuid),
            )
        except Exception:
            logger.exception(
                "Failed to send new ticket notification for issue %s", issue.key
            )


@shared_task(name="waldur_mastermind.support.notify_provider_ticket_withdrawn")
def notify_provider_ticket_withdrawn(
    issue_id, helpdesk_id, child_uuid=None, child_key=None
):
    """Notify a provider that a ticket previously routed to them was rerouted away.

    Sent to the OLD helpdesk after a reroute, so backends that cannot retract the
    external ticket (email/smax) know to drop it. The child issue has already been
    removed, so its uuid and key are passed in: the provider only ever saw the
    child, so this mail belongs in the child's thread and under the child's key,
    the way every other mail they got about that ticket was. Both default to the
    operator (parent) issue for a task enqueued before this argument existed.
    """
    try:
        issue = models.Issue.objects.get(id=issue_id)
        helpdesk = models.ProviderHelpdesk.objects.get(id=helpdesk_id)
    except (models.Issue.DoesNotExist, models.ProviderHelpdesk.DoesNotExist):
        return

    if not helpdesk.notify_on_new_ticket:
        return

    thread_uuid = uuid.UUID(child_uuid) if child_uuid else issue.uuid

    recipients = helpdesk.get_notification_emails()
    if recipients:
        try:
            broadcast_mail(
                "support",
                "provider_ticket_withdrawn",
                {
                    "issue": issue,
                    "provider_helpdesk": helpdesk,
                    "child_key": child_key or issue.key,
                },
                recipients,
                headers=get_issue_thread_headers(thread_uuid),
            )
        except Exception:
            logger.exception(
                "Failed to send ticket withdrawn notification for issue %s", issue.key
            )


@shared_task(name="waldur_mastermind.support.notify_provider_customer_comment")
def notify_provider_customer_comment(comment_id):
    """Notify provider when customer adds a comment."""
    try:
        comment = models.Comment.objects.select_related(
            "issue", "issue__provider_helpdesk"
        ).get(id=comment_id)
    except models.Comment.DoesNotExist:
        return

    issue = comment.issue
    helpdesk = issue.provider_helpdesk
    if not helpdesk or not helpdesk.notify_on_comment:
        return

    recipients = helpdesk.get_notification_emails()
    if recipients:
        try:
            broadcast_mail(
                "support",
                "provider_customer_comment",
                {"issue": issue, "comment": comment, "provider_helpdesk": helpdesk},
                recipients,
                headers=get_issue_thread_headers(issue.uuid),
            )
        except Exception:
            logger.exception(
                "Failed to send comment notification for issue %s", issue.key
            )


@shared_task(name="waldur_mastermind.support.notify_provider_sla_warning")
def notify_provider_sla_warning(issue_id):
    """Notify provider about SLA approaching deadline."""
    try:
        issue = models.Issue.objects.select_related("provider_helpdesk").get(
            id=issue_id
        )
    except models.Issue.DoesNotExist:
        return

    helpdesk = issue.provider_helpdesk
    if not helpdesk or not helpdesk.notify_on_sla_warning:
        return

    recipients = helpdesk.get_notification_emails()
    if recipients:
        try:
            broadcast_mail(
                "support",
                "provider_sla_warning",
                {"issue": issue, "provider_helpdesk": helpdesk},
                recipients,
                headers=get_issue_thread_headers(issue.uuid),
            )
        except Exception:
            logger.exception(
                "Failed to send SLA warning notification for issue %s", issue.key
            )


@shared_task(name="waldur_mastermind.support.notify_ticket_escalated")
def notify_ticket_escalated(issue_id, reason):
    """Notify operator staff that a ticket has been escalated.

    Only for the built-in service desk, for the same reason as
    `notify_staff_new_issue`: Atlassian, Zammad and SMAX reach their agents in
    their own system. `notify_provider_escalation` tells the provider side.
    """
    try:
        issue = models.Issue.objects.get(id=issue_id)
    except models.Issue.DoesNotExist:
        return

    logger.info("Issue %s has been escalated. Reason: %s", issue.key, reason)

    if config.WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE != backend.SupportBackendType.BASIC:
        return

    recipients = get_helpdesk_personnel_emails()
    if not recipients:
        logger.info(
            "No staff or support user is available to notify about the escalation of issue %s.",
            issue.key,
        )
        return

    try:
        broadcast_mail(
            "support",
            "notification_issue_escalated",
            {"issue": issue, "reason": reason},
            recipients,
            headers=get_issue_thread_headers(issue.uuid),
        )
    except Exception:
        logger.exception(
            "Failed to send escalation notification for issue %s", issue.key
        )


@shared_task(name="waldur_mastermind.support.notify_provider_escalation")
def notify_provider_escalation(issue_id, reason):
    """Notify provider that a ticket has been escalated."""
    try:
        issue = models.Issue.objects.get(id=issue_id)
    except models.Issue.DoesNotExist:
        return

    # Notify provider via their helpdesk
    for child in issue.child_issues.select_related("provider_helpdesk").all():
        if child.provider_helpdesk:
            recipients = child.provider_helpdesk.get_notification_emails()
            if recipients and child.provider_helpdesk.notify_on_escalation:
                try:
                    broadcast_mail(
                        "support",
                        "provider_escalation",
                        {
                            "issue": issue,
                            "child_issue": child,
                            "reason": reason,
                        },
                        recipients,
                        headers=get_issue_thread_headers(child.uuid),
                    )
                except Exception:
                    logger.exception(
                        "Failed to send escalation notification for issue %s",
                        issue.key,
                    )


@shared_task(name="waldur_mastermind.support.forward_comment_to_child")
def forward_comment_to_child(comment_id):
    """Forward a comment from parent issue to its child issues."""
    try:
        comment = models.Comment.objects.select_related("issue", "author").get(
            id=comment_id
        )
    except models.Comment.DoesNotExist:
        logger.error("Comment %s not found for forwarding.", comment_id)
        return

    parent_issue = comment.issue
    child_issues = parent_issue.child_issues.all()

    for child_issue in child_issues:
        child_comment = models.Comment.objects.create(
            issue=child_issue,
            author=comment.author,
            description=comment.description,
            is_public=comment.is_public,
            is_forwarded=True,
        )
        logger.info(
            "Forwarded comment from %s to child issue %s.",
            parent_issue.key,
            child_issue.key,
        )
        # The provider reads the forwarded copy in their own ticket, but only
        # learns it is there if they are told.
        notify_provider_customer_comment.delay(child_comment.id)


@shared_task(name="waldur_mastermind.support.propagate_comment_to_parent")
def propagate_comment_to_parent(comment_id):
    """Propagate a comment from child issue back to its parent issue."""
    try:
        comment = models.Comment.objects.select_related("issue", "author").get(
            id=comment_id
        )
    except models.Comment.DoesNotExist:
        logger.error("Comment %s not found for propagation.", comment_id)
        return

    child_issue = comment.issue
    parent_issue = child_issue.parent_issue

    if not parent_issue:
        return

    models.Comment.objects.create(
        issue=parent_issue,
        author=comment.author,
        description=comment.description,
        is_public=comment.is_public,
        is_forwarded=True,
    )
    logger.info(
        "Propagated comment from child %s to parent %s.",
        child_issue.key,
        parent_issue.key,
    )


@shared_task(
    name="waldur_mastermind.support.forward_parent_attachments",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def forward_parent_attachments(self, child_issue_id):
    """Copy the parent's attachments to a child issue routing has created.

    The customer's files belong with the ticket, as its description does. On
    a reroute, those an earlier provider sent back stay behind, as its
    comments do.
    """
    try:
        child_issue = models.Issue.objects.select_related(
            "parent_issue", "provider_helpdesk"
        ).get(id=child_issue_id)
    except models.Issue.DoesNotExist:
        # Withdrawn by a reroute before its files were copied.
        return

    parent_issue = child_issue.parent_issue
    if not parent_issue:
        return

    _forward_attachments(
        self,
        parent_issue,
        [(attachment, child_issue) for attachment in parent_issue.attachments.all()],
    )


@shared_task(
    name="waldur_mastermind.support.forward_attachment_to_child",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def forward_attachment_to_child(self, attachment_id):
    """Forward an attachment from a parent issue to its child issues.

    The children are looked up here rather than when the file was saved: a
    file saved while routing is still creating the child would otherwise
    reach neither.
    """
    try:
        attachment = models.Attachment.objects.select_related("issue").get(
            id=attachment_id
        )
    except models.Attachment.DoesNotExist:
        logger.error("Attachment %s not found for forwarding.", attachment_id)
        return

    parent_issue = attachment.issue
    children = parent_issue.child_issues.select_related("provider_helpdesk")
    _forward_attachments(
        self, parent_issue, [(attachment, child_issue) for child_issue in children]
    )


@shared_task(
    name="waldur_mastermind.support.propagate_attachment_to_parent",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def propagate_attachment_to_parent(self, attachment_id):
    """Propagate an attachment from a child issue back to its parent issue.

    The copy is pushed to the parent's own backend, which comments
    propagated back are not: a remote service desk's sync drops attachments
    it does not hold, so a copy kept only here would vanish from the
    customer's ticket.
    """
    try:
        attachment = models.Attachment.objects.select_related(
            "issue__parent_issue"
        ).get(id=attachment_id)
    except models.Attachment.DoesNotExist:
        logger.error("Attachment %s not found for propagation.", attachment_id)
        return

    parent_issue = attachment.issue.parent_issue
    if not parent_issue or backend.is_orphaned(attachment.issue):
        return

    _forward_attachments(self, parent_issue, [(attachment, parent_issue)])


@shared_task(name="waldur_mastermind.support.retry_attachment_forwarding")
def retry_attachment_forwarding():
    """Push again the copies the forwarding tasks could not deliver.

    A copy left waiting once a task's retries are spent, because its desk was
    down for longer, or left claimed by a worker that died mid-push, would
    otherwise stay hidden and off its desk for good. Each is pushed again
    until it has had ``MAX_PUSH_ATTEMPTS`` pushes, and is then given up as
    ``ERRED`` and noted on the processing log of the routed ticket.
    """
    if not config.WALDUR_SUPPORT_ENABLED:
        return
    if not models.Issue.objects.filter(
        Q(parent_issue__isnull=False) | Q(provider_helpdesk__isnull=False)
    ).exists():
        # Nothing was ever routed, so nothing was forwarded. A routed ticket
        # whose parent was deleted still has its helpdesk.
        return

    now = timezone.now()
    stuck = models.Attachment.objects.filter(is_forwarded=True).filter(
        Q(state=CoreStates.CREATION_SCHEDULED, modified__lt=now - PUSH_RETRY_PAUSE)
        | Q(state=CoreStates.CREATING, modified__lt=now - push_claim_timeout())
    )
    stuck = stuck.select_related("issue__parent_issue").order_by("modified")
    for forwarded in stuck[:PUSH_RETRY_BATCH]:
        issue = forwarded.issue
        if backend.is_orphaned(issue):
            _give_up(forwarded, "helpdesk_deleted")
            continue
        try:
            push_copy(forwarded, backend.get_backend_for_issue(issue))
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            logger.exception(
                "Failed to push attachment %s to issue %s again.",
                forwarded.uuid.hex,
                issue.key,
            )
        try:
            forwarded.refresh_from_db(fields=["state", "push_attempts"])
        except models.Attachment.DoesNotExist:
            # Deleted, with its ticket or by hand, while this run went on.
            continue
        if forwarded.state == CoreStates.ERRED:
            _note_not_forwarded(
                issue.parent_issue or issue,
                [
                    (
                        forwarded.forwarded_from,
                        issue,
                        forwarded,
                        _reason_erred(forwarded),
                    )
                ],
            )


@shared_task(name="waldur_mastermind.support.check_sla_breaches")
def check_sla_breaches():
    """Mark issues as SLA-breached if deadlines have passed."""
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    now = timezone.now()
    breached_issues = models.Issue.objects.filter(
        sla_breached=False,
        resolution_date__isnull=True,
    ).filter(
        Q(first_response_deadline__lt=now, first_response_at__isnull=True)
        | Q(resolution_deadline__lt=now)
    )

    count = breached_issues.update(sla_breached=True)
    if count:
        logger.info("Marked %d issues as SLA-breached.", count)


@shared_task(name="waldur_mastermind.support.check_sla_warnings")
def check_sla_warnings():
    """Log warnings for issues approaching SLA deadlines."""
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    now = timezone.now()
    warning_window = timedelta(hours=1)

    approaching = models.Issue.objects.filter(
        sla_breached=False,
        resolution_date__isnull=True,
    ).filter(
        Q(
            first_response_deadline__lt=now + warning_window,
            first_response_deadline__gt=now,
            first_response_at__isnull=True,
        )
        | Q(
            resolution_deadline__lt=now + warning_window,
            resolution_deadline__gt=now,
        )
    )

    for issue in approaching:
        logger.warning("Issue %s is approaching SLA deadline.", issue.key or issue.uuid)
        if issue.provider_helpdesk_id:
            notify_provider_sla_warning.delay(issue.id)
