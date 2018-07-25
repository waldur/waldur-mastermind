from django.utils.translation import ugettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core import mixins as core_mixins
from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import metadata as structure_metadata
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions

from . import serializers, executors, filters, models


class PlaybookViewSet(core_views.ActionsViewSet):
    lookup_field = 'uuid'
    queryset = models.Playbook.objects.all().order_by('pk')
    unsafe_methods_permissions = [structure_permissions.is_staff]
    serializer_class = serializers.PlaybookSerializer


def check_all_related_resource_are_stable(job):
    States = structure_models.NewResource.States
    stable_states = (States.OK, States.ERRED)
    if not all(resource.state in stable_states for resource in job.get_related_resources()):
        raise core_exceptions.IncorrectStateException(_('Related resources are not stable yet. '
                                                        'Please wait until provisioning is completed.'))


class JobViewSet(core_mixins.CreateExecutorMixin, core_views.ActionsViewSet):
    lookup_field = 'uuid'
    queryset = models.Job.objects.all().order_by('pk')
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filter_class = filters.AnsibleJobsFilter
    unsafe_methods_permissions = [structure_permissions.is_administrator]
    serializer_class = serializers.JobSerializer
    metadata_class = structure_metadata.ActionsMetadata
    create_executor = executors.RunJobExecutor

    destroy_validators = [
        check_all_related_resource_are_stable,
        core_validators.StateValidator(models.Job.States.OK, models.Job.States.ERRED)
    ]
    delete_executor = executors.DeleteJobExecutor
