import logging
from smtplib import SMTPException

from celery import shared_task
from constance import config
from django.conf import settings
from django.core import signing
from django.template import Context, Template
from django.template.loader import get_template

from waldur_core.core import utils as core_utils

from . import backend, models
from .utils import get_feedback_link

logger = logging.getLogger(__name__)


@shared_task(name='waldur_mastermind.support.pull_support_users')
def pull_support_users():
    if not settings.WALDUR_SUPPORT['ENABLED']:
        return

    backend.get_active_backend().pull_support_users()


@shared_task(name='waldur_mastermind.support.pull_priorities')
def pull_priorities():
    if not settings.WALDUR_SUPPORT['ENABLED']:
        return

    backend.get_active_backend().pull_priorities()


@shared_task(name='waldur_mastermind.support.create_issue')
def create_issue(serialized_issue):
    issue = core_utils.deserialize_instance(serialized_issue)
    try:
        backend.get_active_backend().create_issue(issue)
    except Exception as e:
        issue.error_message = str(e)
        issue.save(update_fields=['error_message'])
    else:
        issue.error_message = ''
        issue.save(update_fields=['error_message'])


@shared_task(name='waldur_mastermind.support.create_confirmation_comment')
def create_confirmation_comment(serialized_issue, comment_tmpl=''):
    issue = core_utils.deserialize_instance(serialized_issue)
    try:
        backend.get_active_backend().create_confirmation_comment(issue, comment_tmpl)
    except Exception as e:
        issue.error_message = str(e)
        issue.save(update_fields=['error_message'])
    else:
        issue.error_message = ''
        issue.save(update_fields=['error_message'])


@shared_task(name='waldur_mastermind.support.send_issue_updated_notification')
def send_issue_updated_notification(serialized_issue, changed):
    issue = core_utils.deserialize_instance(serialized_issue)

    _send_issue_notification(
        issue=issue,
        template='issue_updated',
        extra_context={'changed': changed},
    )


@shared_task(name='waldur_mastermind.support.send_comment_added_notification')
def send_comment_added_notification(serialized_comment):
    comment = core_utils.deserialize_instance(serialized_comment)

    _send_issue_notification(
        issue=comment.issue,
        template='comment_added',
        extra_context={'comment': comment},
    )


@shared_task(name='waldur_mastermind.support.send_comment_updated_notification')
def send_comment_updated_notification(serialized_comment, old_description):
    comment = core_utils.deserialize_instance(serialized_comment)

    _send_issue_notification(
        issue=comment.issue,
        template='comment_updated',
        extra_context={
            'comment': comment,
            'old_description': old_description,
        },
    )


def _send_email(
    issue,
    html_template,
    text_template,
    subject_template,
    receiver=None,
    extra_context=None,
):
    if not settings.WALDUR_SUPPORT['ENABLED']:
        return

    if settings.SUPPRESS_NOTIFICATION_EMAILS:
        message = (
            'Issue notifications are suppressed. '
            'Please set SUPPRESS_NOTIFICATION_EMAILS to False to send notifications.'
        )
        logger.info(message)
        return

    if not receiver:
        receiver = issue.caller

    context = {
        'issue_url': core_utils.format_homeport_link(
            'support/issue/{uuid}/', uuid=issue.uuid
        ),
        'site_name': config.SITE_NAME,
        'issue': issue,
    }

    if extra_context:
        context.update(extra_context)

    html_message = html_template.render(Context(context))
    text_message = text_template.render(Context(context, autoescape=False))
    subject = subject_template.render(Context(context, autoescape=False)).strip()

    logger.info('About to send an issue update notification to %s' % receiver.email)

    try:
        core_utils.send_mail(
            subject,
            text_message,
            [receiver.email],
            html_message=html_message,
        )
    except SMTPException as e:
        message = (
            'Failed to notify a user about an issue update. Issue uuid: %s. Error: %s'
            % (issue.uuid.hex, e.message)
        )
        logger.warning(message)


def _send_issue_notification(issue, template, *args, **kwargs):
    try:
        notification_template = models.TemplateStatusNotification.objects.get(
            status=issue.status
        )
        html_template = Template(notification_template.html)
        text_template = Template(notification_template.text)
        subject_template = Template(notification_template.subject)
    except models.TemplateStatusNotification.DoesNotExist:
        html_template = get_template('support/notification_%s.html' % template).template
        text_template = get_template('support/notification_%s.txt' % template).template
        subject_template = get_template(
            'support/notification_%s_subject.txt' % template
        ).template
    _send_email(issue, html_template, text_template, subject_template, *args, **kwargs)


def _send_issue_feedback(issue, template, *args, **kwargs):
    html_template = get_template('support/notification_%s.html' % template).template
    text_template = get_template('support/notification_%s.txt' % template).template
    subject_template = get_template(
        'support/notification_%s_subject.txt' % template
    ).template
    _send_email(issue, html_template, text_template, subject_template, *args, **kwargs)


@shared_task(name='waldur_mastermind.support.send_issue_feedback_notification')
def send_issue_feedback_notification(serialized_issue):
    issue = core_utils.deserialize_instance(serialized_issue)
    signer = signing.TimestampSigner()
    token = signer.sign(issue.uuid.hex)
    extra_context = {
        'feedback_link': get_feedback_link(token),
        'feedback_links': [
            {
                'label': value,
                'link': get_feedback_link(token, key),
            }
            for (key, value) in models.Feedback.Evaluation.CHOICES
        ],
    }
    _send_issue_feedback(
        issue=issue,
        template='issue_feedback',
        extra_context=extra_context,
    )


@shared_task(name='waldur_mastermind.support.sync_feedback')
def sync_feedback(serialized_feedback):
    feedback = core_utils.deserialize_instance(serialized_feedback)
    feedback.state = feedback.States.CREATING
    feedback.save()
    backend.get_active_backend().create_feedback(feedback)


@shared_task(name='waldur_mastermind.support.sync_request_types')
def sync_request_types():
    if not settings.WALDUR_SUPPORT['ENABLED']:
        return

    backend.get_active_backend().pull_request_types()
