import logging

from django.contrib.contenttypes.models import ContentType
from django.template import Context
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from jira import JIRAError
from rest_framework import exceptions as rf_exceptions

from waldur_core.core.utils import format_homeport_link
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.utils import format_limits_list, get_order_url
from waldur_mastermind.support import backend as support_backend
from waldur_mastermind.support import exceptions as support_exceptions
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import serializers as support_serializers

logger = logging.getLogger(__name__)


def get_order_issue(order):
    order_content_type = ContentType.objects.get_for_model(order)
    return support_models.Issue.objects.get(
        resource_object_id=order.id,
        resource_content_type=order_content_type,
    )


def get_request_link(resource: marketplace_models.Resource):
    return format_homeport_link(
        'projects/{project_uuid}/support/{request_uuid}/',
        project_uuid=resource.project.uuid,
        request_uuid=resource.uuid,
    )


def format_description(template_name, context):
    template = get_template('marketplace_support/' + template_name + '.txt')
    return template.template.render(Context(context, autoescape=False))


def format_create_description(order):
    result = []

    for key in order.offering.options.get('order') or []:
        if key not in order.attributes:
            continue

        label = order.offering.options['options'].get(key, {})
        label_value = label.get('label', key)
        result.append(f'{label_value}: \'{order.attributes[key]}\'')

    if 'description' in order.attributes:
        result.append('\n %s' % order.attributes['description'])

    result.append(
        format_description(
            'create_resource_template',
            {
                'order': order,
                'order_url': get_order_url(order),
                'resource': order.resource,
            },
        )
    )

    if order.limits:
        components_map = order.offering.get_limit_components()
        for key, value in order.limits.items():
            component = components_map.get(key)
            if component:
                result.append(
                    "\n{} ({}): {} {}".format(
                        component.name,
                        component.type,
                        value,
                        component.measured_unit,
                    )
                )

    description = '\n'.join(result)

    return description


def create_issue(order, description, summary, confirmation_comment=None):
    order_content_type = ContentType.objects.get_for_model(order)
    active_backend = support_backend.get_active_backend()

    if support_models.Issue.objects.filter(
        resource_object_id=order.id, resource_content_type=order_content_type
    ).exists():
        logger.warning(
            'An issue creating is skipped because an issue for order %s exists already.',
            order.uuid,
        )
        return

    issue_details = active_backend.get_issue_details()

    issue_details.update(
        dict(
            caller=order.created_by,
            project=order.project,
            customer=order.project.customer,
            description=description,
            summary=summary,
            resource=order,
        )
    )
    issue_details['summary'] = support_serializers.render_issue_template(
        'ATLASSIAN_SUMMARY_TEMPLATE', 'summary', issue_details
    )
    issue_details['description'] = support_serializers.render_issue_template(
        'ATLASSIAN_DESCRIPTION_TEMPLATE', 'description', issue_details
    )
    issue = support_models.Issue.objects.create(**issue_details)
    try:
        active_backend.create_issue(issue)
    except support_exceptions.SupportUserInactive:
        issue.delete()
        order.resource.set_state_erred()
        order.resource.save(update_fields=['state'])
        raise rf_exceptions.ValidationError(
            _(
                'Delete resource process is cancelled and issue not created '
                'because a caller is inactive.'
            )
        )
    except ServiceBackendError as e:
        issue.delete()
        order.resource.set_state_erred()
        order.resource.save(update_fields=['state'])
        raise rf_exceptions.ValidationError(e)

    ids = marketplace_models.Order.objects.filter(resource=order.resource).values_list(
        'id', flat=True
    )
    linked_issues = support_models.Issue.objects.filter(
        resource_object_id__in=ids,
        resource_content_type=order_content_type,
    ).exclude(id=issue.id)
    try:
        active_backend.create_issue_links(issue, list(linked_issues))
    except JIRAError as e:
        logger.exception('Linked issues have not been added: %s', e)

    if confirmation_comment:
        try:
            active_backend.create_confirmation_comment(issue, confirmation_comment)
        except JIRAError as e:
            logger.exception('Unable to create confirmation comment: %s', e)

    return issue


def format_update_description(order):
    request_url = get_request_link(order.resource)
    return format_description(
        'update_resource_template',
        {'order': order, 'request_url': request_url},
    )


def format_update_limits_description(order):
    offering = order.resource.offering
    request_url = get_request_link(order.resource)
    components_map = offering.get_limit_components()
    old_limits = format_limits_list(components_map, order.resource.limits)
    new_limits = format_limits_list(components_map, order.limits)
    context = {
        'order': order,
        'request_url': request_url,
        'old_limits': old_limits,
        'new_limits': new_limits,
    }
    return format_description(
        'update_limits_template',
        context,
    )


def format_delete_description(order):
    request_url = get_request_link(order.resource)
    return format_description(
        'terminate_resource_template',
        {'order': order, 'request_url': request_url},
    )
