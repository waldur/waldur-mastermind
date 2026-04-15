import logging
from smtplib import SMTPException

from celery import shared_task

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import send_mail
from waldur_mastermind.marketplace_support import utils

logger = logging.getLogger(__name__)


@shared_task
def send_mail_notification(subject, message, to):
    try:
        send_mail(subject, message, [to])
    except SMTPException:
        message = "Failed to send email. Receiver email: %s." % to
        logger.warning(message)


@shared_task
def create_issue_for_pending_order(serialized_order):
    order = core_utils.deserialize_instance(serialized_order)
    issue = utils.create_issue(
        order,
        summary=f"Request for {order.offering.name}",
        description=utils.format_create_description(order),
        confirmation_comment=order.offering.secret_options.get(
            "template_confirmation_comment"
        ),
    )

    if issue:
        resource = order.resource
        resource.scope = issue
        resource.backend_id = issue.backend_id or ""
        resource.save()
