import logging

from django.db import transaction
from django.template import Context, Template

from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support import PLUGIN_NAME

from . import tasks

logger = logging.getLogger(__name__)


ItemTypes = marketplace_models.OrderItem.Types


RESOURCE_CALLBACKS = {
    (ItemTypes.CREATE, True): callbacks.resource_creation_succeeded,
    (ItemTypes.CREATE, False): callbacks.resource_creation_failed,
    (ItemTypes.UPDATE, True): callbacks.resource_update_succeeded,
    (ItemTypes.UPDATE, False): callbacks.resource_update_failed,
    (ItemTypes.TERMINATE, True): callbacks.resource_deletion_succeeded,
    (ItemTypes.TERMINATE, False): callbacks.resource_deletion_failed,
}


def update_order_item_if_issue_was_complete(sender, instance, created=False, **kwargs):
    if created:
        return

    issue = instance

    if not issue.tracker.has_changed('status'):
        return

    if not (
        issue.resource
        and isinstance(issue.resource, marketplace_models.OrderItem)
        and issue.resource.offering.type == PLUGIN_NAME
        and issue.resolved is not None
    ):
        return

    callback = RESOURCE_CALLBACKS[(issue.resource.type, issue.resolved)]
    callback(issue.resource.resource)


def notify_about_request_based_item_creation(sender, instance, created=False, **kwargs):
    if created:
        return

    issue = instance

    if not issue.tracker.has_changed('backend_id'):
        return

    if not (
        issue.resource
        and isinstance(issue.resource, marketplace_models.OrderItem)
        and issue.resource.offering.type == PLUGIN_NAME
        and issue.resource.type == ItemTypes.CREATE
    ):
        return

    order_item = issue.resource
    service_provider = getattr(order_item.offering.customer, 'serviceprovider', None)

    if not service_provider:
        logger.warning(
            'Customer providing an Offering is not registered as a Service Provider.'
        )
        return

    if not service_provider.lead_email:
        return

    attributes_with_display_names = {}

    for attribute_key, attribute_value in order_item.attributes.items():
        if attribute_key in order_item.offering.options['options'].keys():
            display_name = order_item.offering.options['options'][attribute_key][
                'label'
            ]
            attributes_with_display_names[display_name] = attribute_value
            continue

        attributes_with_display_names[attribute_key] = attribute_value

    setattr(order_item, 'attributes_with_display_names', attributes_with_display_names)

    context = Context({'order_item': order_item, 'issue': issue}, autoescape=False)
    template = Template(service_provider.lead_body)
    message = template.render(context).strip()
    template = Template(service_provider.lead_subject)
    subject = template.render(context).strip()

    transaction.on_commit(
        lambda: tasks.send_mail_notification.delay(
            subject, message, service_provider.lead_email
        )
    )
