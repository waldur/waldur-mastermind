import copy
import logging
from smtplib import SMTPException

from celery import shared_task
from constance import config
from django.core import signing
from django.template import Context, Template
from django.template.loader import get_template

import html2text
import textile
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils

from . import backend, models
from .utils import get_feedback_link

logger = logging.getLogger(__name__)


@shared_task(name="waldur_mastermind.support.pull_support_users")
def pull_support_users():
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    backend.get_active_backend().pull_support_users()


@shared_task(name="waldur_mastermind.support.pull_priorities")
def pull_priorities():
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
    # which is used to identify the notification..
    if notification_key:
        try:
            notification = core_models.Notification.objects.get(key=notification_key)
            if not notification.enabled:
                message = (
                    "Notification %s is disabled. ",
                    "Please enable it to send notifications." % notification_key,
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
                text_context[k.replace("format_", "")] = html2text.html2text(
                    text_context[k]
                )
            else:
                text_context[k.replace("format_", "")] = text_context[k]

    for k in list(html_context):
        if k.startswith("format_"):
            if not html_format:
                html_context[k.replace("format_", "")] = textile.textile(
                    html_context[k]
                )
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
        message = f"Failed to notify a user about an issue update. Issue uuid: {issue.uuid.hex}. Error: {e.message}"
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
        html_template = get_template("support/notification_%s.html" % template).template
        text_template = get_template("support/notification_%s.txt" % template).template
        subject_template = get_template(
            "support/notification_%s_subject.txt" % template
        ).template
    _send_email(issue, html_template, text_template, subject_template, *args, **kwargs)


def _send_issue_feedback(issue, template, *args, **kwargs):
    html_template = get_template("support/notification_%s.html" % template).template
    text_template = get_template("support/notification_%s.txt" % template).template
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
                "label": value,
                "link": get_feedback_link(token, key),
            }
            for (key, value) in models.Feedback.Evaluation.CHOICES
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
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    active_backend = backend.get_active_backend()

    if not hasattr(active_backend, "pull_request_types"):
        return

    backend.get_active_backend().pull_request_types()


@shared_task(name="waldur_mastermind.support.sync_issues")
def sync_issues():
    backend.get_active_backend().sync_issues()
