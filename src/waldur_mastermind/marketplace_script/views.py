from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core.views import ActionsViewSet
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace.permissions import user_is_owner_or_service_manager

from . import PLUGIN_NAME
from .serializers import DryRunSerializer
from .utils import ContainerExecutorMixin


class DryRunView(ActionsViewSet):
    queryset = marketplace_models.Offering.objects.filter(type=PLUGIN_NAME)
    lookup_field = 'uuid'
    serializer_class = marketplace_serializers.OfferingDetailsSerializer
    disabled_actions = [
        'retrieve',
        'list',
        'create',
        'update',
        'partial_update',
        'destroy',
    ]

    """
    Example of usage is below. "type" field can have of the following values: Create, Update, Terminate

    .. code-block:: http

        POST http://127.0.0.1:8000/api/marketplace-script-dry-run/5329a7bef29a44d29c5f4230dc1ed00e/run/
        Content-Type: application/json
        Authorization: Token 154f2c6984b5992928b62f87950ac529f1f906ca
        Accept: application/json

        {
            "plan": "http://127.0.0.1:8000/api/marketplace-plans/ed80e1047eeb4c9eb67f6e61b98977bc/",
            "type": "Update"
        }
    """

    @action(detail=True, methods=['post'])
    def run(self, request, *args, **kwargs):
        serializer = DryRunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering = self.get_object()
        order_item = marketplace_models.OrderItem(**serializer.validated_data)
        order_item.offering = offering
        order_item_type = order_item.get_type_display().lower()

        project = structure_models.Project(
            name='Dry-run project', customer=offering.customer
        )
        order = marketplace_models.Order(
            created_by=request.user,
            project=project,
        )

        order_item.order = order

        executor = ContainerExecutorMixin()
        executor.order_item = order_item
        executor.hook_type = order_item_type
        output = executor.send_request(request.user, dry_run=True)
        return Response({'output': output})

    run_permissions = [user_is_owner_or_service_manager]
