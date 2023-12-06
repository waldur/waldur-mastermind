from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from waldur_core.core.views import ActionsViewSet, APIView, ReadOnlyActionsViewSet
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace_script import (
    executors as marketplace_script_executors,
)
from waldur_mastermind.marketplace_script import models as marketplace_script_models
from waldur_mastermind.marketplace_script import (
    serializers as marketplace_script_serializers,
)

from . import PLUGIN_NAME, tasks
from .serializers import DryRunSerializer, DryRunTypes
from .utils import ContainerExecutorMixin


class DryRunView(ActionsViewSet):
    queryset = marketplace_models.Offering.objects.filter(type=PLUGIN_NAME)
    lookup_field = 'uuid'
    serializer_class = marketplace_serializers.PublicOfferingDetailsSerializer
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
        order = marketplace_models.Order(**serializer.validated_data)
        order.offering = offering
        order_type = DryRunTypes.get_type_display(order.type)
        script_language = order.offering.secret_options.get('language')
        if not script_language:
            return Response(
                {
                    'Can not dry run the script. The script language is not set.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        project = structure_models.Project.objects.create(
            name='Dry-run project', customer=offering.customer
        )
        order.created_by = request.user
        order.project = project
        order.save()

        executor = ContainerExecutorMixin()
        executor.order = order
        executor.hook_type = order_type
        output = executor.send_request(request.user, dry_run=True)
        return Response({'output': output})

    @action(detail=True, methods=['post'])
    def async_run(self, request, *args, **kwargs):
        serializer = DryRunSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offering = self.get_object()
        project = structure_models.Project.objects.create(
            name='Dry-run project', customer=offering.customer
        )
        order = marketplace_models.Order(**serializer.validated_data)
        order.offering = offering
        order_type = DryRunTypes.get_type_display(order.type)
        order.created_by = request.user
        order.project = project
        order.save()
        dry_run = marketplace_script_models.DryRun.objects.create(
            order=order,
            order_type=order_type,
            order_offering=order.offering,
            order_attributes=order.attributes,
            order_plan=order.plan,
        )
        transaction.on_commit(
            lambda: marketplace_script_executors.DryRunExecutor.execute(dry_run)
        )
        return Response({'uuid': dry_run.uuid.hex}, status=status.HTTP_202_ACCEPTED)

    run_permissions = async_run_permissions = [
        permission_factory(PermissionEnum.DRY_RUN_OFFERING_SCRIPT, ['*', 'customer'])
    ]


class AsyncDryRunView(ReadOnlyActionsViewSet):
    queryset = marketplace_script_models.DryRun.objects.filter().order_by('-created')
    lookup_field = 'uuid'
    filter_backends = (structure_filters.GenericRoleFilter,)
    serializer_class = DryRunSerializer


class PullMarketplaceScriptResourceView(APIView):
    def post(self, request, *args, **kwargs):
        serializer = (
            marketplace_script_serializers.PullMarketplaceScriptResourceSerializer(
                data=request.data
            )
        )
        serializer.is_valid(raise_exception=True)
        resource_uuid = serializer.validated_data['resource_uuid']

        try:
            queryset = marketplace_models.Resource.objects.filter(uuid=resource_uuid)
        except ObjectDoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        allowed_resource = queryset.filter_for_user(request.user)
        if not allowed_resource:
            return Response(status=status.HTTP_404_NOT_FOUND)

        tasks.pull_resource.delay(allowed_resource.first().id)
        return Response(
            {'detail': _('Pull operation was successfully scheduled.')},
            status=status.HTTP_202_ACCEPTED,
        )
