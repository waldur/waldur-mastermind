from __future__ import unicode_literals

import logging
from smtplib import SMTPException

from celery import shared_task
from celery.task import Task as CeleryTask
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import get_template
from django.template import Template, Context
from django.utils import timezone

from waldur_core.core import utils as core_utils

from . import backend, models

logger = logging.getLogger(__name__)


class SupportUserPullTask(CeleryTask):
    """
    Pull support users from backend.
    Note that support users are not deleted in JIRA.
    Instead, they are marked as disabled.
    Therefore, Waldur replicates the same behaviour.
    """
    name = 'support.SupportUserPullTask'

    def run(self):
        if not settings.WALDUR_SUPPORT['ENABLED']:
            return

        backend_users = backend.get_active_backend().get_users()
        for backend_user in backend_users:
            user, created = models.SupportUser.objects.get_or_create(
                backend_id=backend_user.backend_id, defaults={'name': backend_user.name})
            if not created and user.name != backend_user.name:
                user.name = backend_user.name
                user.save()
            if not user.is_active:
                user.is_active = True
                user.save()
        models.SupportUser.objects.exclude(backend_id__in=[u.backend_id for u in backend_users])\
            .update(is_active=False)


@shared_task(name='waldur_mastermind.support.create_issue')
def create_issue(serialized_issue):
    issue = core_utils.deserialize_instance(serialized_issue)
    backend.get_active_backend().create_issue(issue)


@shared_task(name='waldur_mastermind.support.send_issue_updated_notification')
def send_issue_updated_notification(serialized_issue):
    issue = core_utils.deserialize_instance(serialized_issue)
    _send_issue_notification(issue, 'issue_updated')


@shared_task(name='waldur_mastermind.support.send_comment_added_notification')
def send_comment_added_notification(serialized_comment):
    comment = core_utils.deserialize_instance(serialized_comment)

    # Skip notifications about comments added to an issue by caller himself
    if comment.author.user != comment.issue.caller:
        _send_issue_notification(comment.issue, 'comment_added')


def _send_issue_notification(issue, template, receiver=None):
    if not settings.WALDUR_SUPPORT['ENABLED']:
        return

    if settings.SUPPRESS_NOTIFICATION_EMAILS:
        message = ('Issue notifications are suppressed. '
                   'Please set SUPPRESS_NOTIFICATION_EMAILS to False to send notifications.')
        logger.info(message)
        return

    if not receiver:
        receiver = issue.caller

    context = {
        'issue_url': settings.ISSUE_LINK_TEMPLATE.format(uuid=issue.uuid),
        'site_name': settings.WALDUR_CORE['SITE_NAME'],
        'issue': issue,
    }

    try:
        notification_template = models.TemplateStatusNotification.objects.get(status=issue.status)
        html_template = Template(notification_template.html)
        text_template = Template(notification_template.text)
        subject_template = Template(notification_template.subject)
    except models.TemplateStatusNotification.DoesNotExist:
        html_template = get_template('support/notification_%s.html' % template).template
        text_template = get_template('support/notification_%s.txt' % template).template
        subject_template = get_template('support/notification_%s_subject.txt' % template).template

    html_message = html_template.render(Context(context))
    text_message = text_template.render(Context(context, autoescape=False))
    subject = subject_template.render(Context(context, autoescape=False)).strip()

    logger.debug('About to send an issue update notification to %s' % receiver.email)

    try:
        send_mail(subject, text_message, settings.DEFAULT_FROM_EMAIL, [receiver.email], html_message=html_message)
    except SMTPException as e:
        message = 'Failed to notify a user about an issue update. Issue uuid: %s. Error: %s' % (issue.uuid, e.message)
        logger.warning(message)


@shared_task(name='waldur_mastermind.support.remove_terminated_offerings')
def remove_terminated_offerings():
    """
    Request based offering lifetime must be specified in Waldur support settings with parameter
    "TERMINATED_OFFERING_LIFETIME". If terminated offering lifetime is expired, offering is removed.
    """
    if not settings.WALDUR_SUPPORT['ENABLED']:
        return

    expiration_date = timezone.now() - settings.WALDUR_SUPPORT['TERMINATED_OFFERING_LIFETIME']
    offerings = models.Offering.objects.filter(
        state=models.Offering.States.TERMINATED,
        terminated_at__lte=expiration_date,
    )
    # Bulk delete all expired offerings
    offerings.delete()
