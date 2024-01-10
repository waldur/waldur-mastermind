import logging

from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.core import mixins as core_mixins
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.models import StateMixin
from waldur_core.structure import permissions as structure_permissions
from waldur_openstack.openstack_base import executors as openstack_base_executors

logger = logging.getLogger(__name__)


class FlavorViewSet(
    core_mixins.CreateExecutorMixin,
    core_mixins.DeleteExecutorMixin,
    core_views.ActionsViewSet,
):
    """
    VM instance flavor is a pre-defined set of virtual hardware parameters that the instance will use:
    CPU, memory, disk size etc. VM instance flavor is not to be confused with VM template -- flavor is a set of virtual
    hardware parameters whereas template is a definition of a system to be installed on this instance.
    """

    queryset = NotImplemented  # models.Flavor.objects.all().order_by('settings', 'cores', 'ram', 'disk')
    serializer_class = NotImplemented  # serializers.FlavorSerializer
    filterset_class = NotImplemented  # filters.FlavorFilter
    lookup_field = "uuid"
    filter_backends = (DjangoFilterBackend,)
    disabled_actions = ["update"]
    create_permissions = [
        structure_permissions.check_access_to_services_management,
    ]
    destroy_permissions = [
        structure_permissions.check_access_to_services_management,
        structure_permissions.is_service_manager,
    ]
    destroy_validators = [
        core_validators.StateValidator(StateMixin.States.OK, StateMixin.States.ERRED)
    ]

    create_executor = openstack_base_executors.FlavorCreateExecutor
    delete_executor = openstack_base_executors.FlavorDeleteExecutor
