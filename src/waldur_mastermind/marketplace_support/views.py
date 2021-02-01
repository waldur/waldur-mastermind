import logging

from rest_framework import status
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support import utils

logger = logging.getLogger(__name__)


class IssueViewSet(core_views.ActionsViewSet):
    def create(self, request, *args, **kwargs):
        order_item = marketplace_models.OrderItem.objects.get(uuid=request.data['uuid'])
        utils.create_issue(
            order_item,
            summary=f'Request for {order_item.offering.name}',
            description=utils.format_create_description(order_item),
            confirmation_comment=order_item.offering.secret_options.get(
                'template_confirmation_comment'
            ),
        )
        return Response(status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        order_item = marketplace_models.OrderItem.objects.get(uuid=request.data['uuid'])
        utils.create_issue(
            order_item,
            description=utils.format_update_description(order_item),
            summary='Request to switch plan for %s' % order_item.resource.name,
        )
        return Response(status=status.HTTP_202_ACCEPTED)

    def destroy(self, request, uuid, *args, **kwargs):
        order_item = marketplace_models.OrderItem.objects.get(uuid=uuid)
        utils.create_issue(
            order_item,
            description=utils.format_delete_description(order_item),
            summary='Request to terminate resource %s' % order_item.resource.name,
        )
        return Response(status=status.HTTP_202_ACCEPTED)
