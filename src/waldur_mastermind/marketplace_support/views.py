import logging

from rest_framework import status
from rest_framework.response import Response

from waldur_core.core import views as core_views
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_support import utils

logger = logging.getLogger(__name__)


class IssueViewSet(core_views.ActionsViewSet):
    def create(self, request, *args, **kwargs):
        order = marketplace_models.Order.objects.get(uuid=request.data['uuid'])
        issue = utils.create_issue(
            order,
            summary=f'Request for {order.offering.name}',
            description=utils.format_create_description(order),
            confirmation_comment=order.offering.secret_options.get(
                'template_confirmation_comment'
            ),
        )
        return Response(status=status.HTTP_201_CREATED, data={'uuid': issue.uuid.hex})

    def update(self, request, *args, **kwargs):
        order = marketplace_models.Order.objects.get(uuid=request.data['uuid'])
        utils.create_issue(
            order,
            description=utils.format_update_description(order),
            summary='Request to switch plan for %s' % order.resource.name,
        )
        return Response(status=status.HTTP_202_ACCEPTED)

    def destroy(self, request, uuid, *args, **kwargs):
        order = marketplace_models.Order.objects.get(uuid=uuid)
        utils.create_issue(
            order,
            description=utils.format_delete_description(order),
            summary='Request to terminate resource %s' % order.resource.name,
        )
        return Response(status=status.HTTP_202_ACCEPTED)
