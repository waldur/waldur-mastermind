import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.template import Context, Template
from django.utils.translation import ugettext_lazy as _
from jira import JIRAError
from rest_framework import exceptions as rf_exceptions

from waldur_core.core.utils import format_homeport_link
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.utils import get_order_item_url
from waldur_mastermind.support import backend as support_backend
from waldur_mastermind.support import exceptions as support_exceptions
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import serializers as support_serializers

logger = logging.getLogger(__name__)


def get_order_item_issue(order_item):
    order_item_content_type = ContentType.objects.get_for_model(order_item)
    return support_models.Issue.objects.get(
        resource_object_id=order_item.id, resource_content_type=order_item_content_type,
    )


def get_request_link(resource: marketplace_models.Resource):
    return format_homeport_link(
        'projects/{project_uuid}/support/{request_uuid}/',
        project_uuid=resource.project.uuid,
        request_uuid=resource.uuid,
    )


def format_description(template_name, context):
    template = Template(settings.WALDUR_MARKETPLACE_SUPPORT[template_name])
    return template.render(Context(context, autoescape=False))


def format_create_description(order_item):
    result = []

    for key in order_item.offering.options.get('order') or []:
        if key not in order_item.attributes:
            continue

        label = order_item.offering.options['options'].get(key, {})
        label_value = label.get('label', key)
        result.append('%s: \'%s\'' % (label_value, order_item.attributes[key]))

    if 'description' in order_item.attributes:
        result.append('\n %s' % order_item.attributes['description'])

    result.append(
        format_description(
            'CREATE_RESOURCE_TEMPLATE',
            {
                'order_item': order_item,
                'order_item_url': get_order_item_url(order_item),
            },
        )
    )

    if order_item.limits:
        components_map = order_item.offering.get_usage_components()
        for key, value in order_item.limits.items():
            component = components_map.get(key)
            if component:
                result.append(
                    "\n%s (%s): %s %s"
                    % (component.name, component.type, value, component.measured_unit,)
                )

    description = '\n'.join(result)

    return description


def create_issue(order_item, description, summary, confirmation_comment=None):
    order_item_content_type = ContentType.objects.get_for_model(order_item)

    if support_models.Issue.objects.filter(
        resource_object_id=order_item.id, resource_content_type=order_item_content_type
    ).exists():
        logger.warning(
            'An issue creating is skipped because an issue for order item %s exists already.'
            % order_item.uuid
        )
        return
    issue_details = dict(
        caller=order_item.order.created_by,
        project=order_item.order.project,
        customer=order_item.order.project.customer,
        type=settings.WALDUR_SUPPORT['DEFAULT_OFFERING_ISSUE_TYPE'],
        description=description,
        summary=summary,
        resource=order_item,
    )
    issue_details['summary'] = support_serializers.render_issue_template(
        'summary', issue_details
    )
    issue_details['description'] = support_serializers.render_issue_template(
        'description', issue_details
    )
    issue = support_models.Issue.objects.create(**issue_details)
    try:
        support_backend.get_active_backend().create_issue(issue)
    except support_exceptions.SupportUserInactive:
        issue.delete()
        order_item.resource.set_state_erred()
        order_item.resource.save(update_fields=['state'])
        raise rf_exceptions.ValidationError(
            _(
                'Delete resource process is cancelled and issue not created '
                'because a caller is inactive.'
            )
        )

    if order_item.resource:
        ids = marketplace_models.OrderItem.objects.filter(
            resource=order_item.resource
        ).values_list('id', flat=True)
        linked_issues = support_models.Issue.objects.filter(
            resource_object_id__in=ids, resource_content_type=order_item_content_type,
        ).exclude(id=issue.id)
        try:
            support_backend.get_active_backend().create_issue_links(
                issue, list(linked_issues)
            )
        except JIRAError as e:
            logger.exception('Linked issues have not been added: %s', e)

    if confirmation_comment:
        try:
            support_backend.get_active_backend().create_confirmation_comment(
                issue, confirmation_comment
            )
        except JIRAError as e:
            logger.exception('Unable to create confirmation comment: %s', e)

    return issue


def format_update_description(order_item):
    request_url = get_request_link(order_item.resource)
    return format_description(
        'UPDATE_RESOURCE_TEMPLATE',
        {'order_item': order_item, 'request_url': request_url},
    )


def format_delete_description(order_item):
    request_url = get_request_link(order_item.resource)
    return format_description(
        'TERMINATE_RESOURCE_TEMPLATE',
        {'order_item': order_item, 'request_url': request_url},
    )
