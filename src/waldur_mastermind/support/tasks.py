import copy
import logging
from datetime import timedelta
from smtplib import SMTPException

from celery import shared_task
from constance import config
from django.core import signing
from django.db.models import Q
from django.template import Context, Template
from django.template.loader import get_template
from django.utils import timezone
from markdownify import markdownify

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.utils import broadcast_mail, text2html

from . import backend, models
from .utils import get_feedback_link

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
    subject = subject_template.render(Context(context, autoescape=False)).strip()

    logger.info("About to send an issue update notification to %s" % receiver.email)

    try:
        core_utils.send_mail(
            subject,
            text_message,
            [receiver.email],
            html_message=html_message,
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
    how to handle it (retry vs. surfacing an error).
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

    return child_issue


def reroute_issue_to_provider(issue, new_helpdesk):
    """Move an already-routed issue from its current provider helpdesk to a new one.

    Tears down the existing child issue(s) through the ORIGINAL provider's backend
    (a real delete for jira/zammad; a no-op for the basic backend, where the child
    row is the whole ticket), then creates a fresh child for ``new_helpdesk``.
    Returns ``(new_child_issue, old_helpdesks)``. The caller runs this inside a
    transaction and dispatches notifications on commit.
    """
    from .backend import get_backend_for_provider

    # Create the new child FIRST. An already-deleted external ticket
    # (jira/zammad) cannot be restored by an ORM rollback, so tearing the old
    # one down before the new routing succeeds would risk losing it when
    # create_provider_child_issue fails. Creating first leaves the original
    # ticket intact on failure.
    new_child = create_provider_child_issue(issue, new_helpdesk, issue.resource)

    old_helpdesks = []
    for child in (
        issue.child_issues.select_related("provider_helpdesk")
        .exclude(id=new_child.id)
        .all()
    ):
        old_helpdesk = child.provider_helpdesk
        if old_helpdesk:
            old_helpdesks.append(old_helpdesk)
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
        child.delete()

    issue.append_processing_log(
        "rerouted_to_provider",
        {
            "child_issue_uuid": new_child.uuid.hex,
            "from": [str(h.service_provider) for h in old_helpdesks],
            "to": str(new_helpdesk.service_provider),
        },
    )
    issue.save(update_fields=["processing_log"])

    return new_child, old_helpdesks


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

    recipients = list(
        core_models.User.objects.filter(is_active=True, notifications_enabled=True)
        .filter(Q(is_staff=True) | Q(is_support=True))
        .exclude(email="")
        .values_list("email", flat=True)
        .distinct()
    )
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
            )
        except Exception:
            logger.exception(
                "Failed to send new ticket notification for issue %s", issue.key
            )


@shared_task(name="waldur_mastermind.support.notify_provider_ticket_withdrawn")
def notify_provider_ticket_withdrawn(issue_id, helpdesk_id):
    """Notify a provider that a ticket previously routed to them was rerouted away.

    Sent to the OLD helpdesk after a reroute, so backends that cannot retract the
    external ticket (email/smax) know to drop it. The child issue has already been
    removed, so this references the operator (parent) issue.
    """
    try:
        issue = models.Issue.objects.get(id=issue_id)
        helpdesk = models.ProviderHelpdesk.objects.get(id=helpdesk_id)
    except (models.Issue.DoesNotExist, models.ProviderHelpdesk.DoesNotExist):
        return

    if not helpdesk.notify_on_new_ticket:
        return

    recipients = helpdesk.get_notification_emails()
    if recipients:
        try:
            broadcast_mail(
                "support",
                "provider_ticket_withdrawn",
                {"issue": issue, "provider_helpdesk": helpdesk},
                recipients,
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
            )
        except Exception:
            logger.exception(
                "Failed to send SLA warning notification for issue %s", issue.key
            )


@shared_task(name="waldur_mastermind.support.notify_ticket_escalated")
def notify_ticket_escalated(issue_id, reason):
    """Notify operator staff that a ticket has been escalated."""
    try:
        issue = models.Issue.objects.get(id=issue_id)
    except models.Issue.DoesNotExist:
        return

    logger.info("Issue %s has been escalated. Reason: %s", issue.key, reason)


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
        models.Comment.objects.create(
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
