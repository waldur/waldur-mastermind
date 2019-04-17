from __future__ import unicode_literals

import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import status
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support.utils import get_request_link, format_description
from waldur_mastermind.support import backend as support_backend
from waldur_mastermind.support import exceptions as support_exceptions
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import serializers as support_serializers

logger = logging.getLogger(__name__)


def create_issue(order_item, description, summary):
    order_item_content_type = ContentType.objects.get_for_model(order_item)

    if not support_models.Issue.objects.filter(resource_object_id=order_item.id,
                                               resource_content_type=order_item_content_type).exists():
        issue_details = dict(
            caller=order_item.order.created_by,
            project=order_item.order.project,
            customer=order_item.order.project.customer,
            type=settings.WALDUR_SUPPORT['DEFAULT_OFFERING_ISSUE_TYPE'],
            description=description,
            summary=summary,
            resource=order_item)
        issue_details['summary'] = support_serializers.render_issue_template('summary', issue_details)
        issue_details['description'] = support_serializers.render_issue_template('description', issue_details)
        issue = support_models.Issue.objects.create(**issue_details)
        try:
            support_backend.get_active_backend().create_issue(issue)
        except support_exceptions.SupportUserInactive:
            issue.delete()
            order_item.resource.set_state_erred()
            order_item.resource.save(update_fields=['state'])
            raise rf_exceptions.ValidationError(_('Delete resource process is cancelled and issue not created '
                                                  'because a caller is inactive.'))
    else:
        message = 'An issue creating is skipped because an issue for order item %s exists already.' % order_item.uuid
        logger.warning(message)


class IssueViewSet(core_views.ActionsViewSet):
    def update(self, request, *args, **kwargs):
        uuid = request.data['uuid']
        order_item = marketplace_models.OrderItem.objects.get(uuid=uuid)
        summary = 'Request to switch plan for %s' % order_item.resource.scope.name
        request_url = get_request_link(order_item.resource.scope)
        description = format_description('UPDATE_RESOURCE_TEMPLATE', {
            'order_item': order_item,
            'request_url': request_url,
        })
        create_issue(order_item, description, summary)
        return Response(status=status.HTTP_202_ACCEPTED)

    def destroy(self, request, uuid, *args, **kwargs):
        order_item = marketplace_models.OrderItem.objects.get(uuid=uuid)
        summary = 'Request to terminate resource %s' % order_item.resource.scope.name
        request_url = get_request_link(order_item.resource.scope)
        description = format_description('TERMINATE_RESOURCE_TEMPLATE', {
            'order_item': order_item,
            'request_url': request_url,
        })
        create_issue(order_item, description, summary)
        return Response(status=status.HTTP_202_ACCEPTED)
