import logging

from rest_framework import decorators, response

from waldur_ansible.common import serializers as common_serializers
from waldur_ansible.python_management import views as python_management_views
from waldur_ansible.python_management import serializers as python_management_serializers

from waldur_core.core import views as core_views, managers as core_managers, mixins as core_mixins, models as core_models
from . import models, serializers, executors, jupyter_hub_management_service

jupyter_hub_management_requests_models = [models.JupyterHubManagementSyncConfigurationRequest,
                                          models.JupyterHubManagementDeleteRequest,
                                          models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest,
                                          models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest]

logger = logging.getLogger(__name__)


class JupyterHubManagementViewSet(core_mixins.AsyncExecutor, core_views.ActionsViewSet):
    lookup_field = 'uuid'
    queryset = models.JupyterHubManagement.objects.all().order_by('pk')
    serializer_class = serializers.JupyterHubManagementSerializer
    python_management_request_executor = executors.JupyterHubManagementRequestExecutor
    service = jupyter_hub_management_service.JupyterHubManagementService()

    def retrieve(self, request, *args, **kwargs):
        jupyter_hub_management = self.get_object()
        jupyter_hub_management_serializer = self.get_serializer(jupyter_hub_management)

        requests = self.find_related_requests(jupyter_hub_management)
        python_management_requests = self.find_related_python_management_requests(jupyter_hub_management)

        python_management = python_management_serializers.PythonManagementSerializer(jupyter_hub_management.python_management, context={'request': request})

        return response.Response(
            {'jupyter_hub_management': jupyter_hub_management_serializer.data,
             'requests': requests,
             'python_management_requests': python_management_requests,
             'selected_python_management': python_management.data})

    def find_related_python_management_requests(self, jupyter_hub_management):
        # we are interested in blocking python management requests (currently running)
        python_management_requests = core_managers.SummaryQuerySet(python_management_views.python_management_requests_models) \
            .filter(python_management=jupyter_hub_management.python_management) \
            .exclude(state__in=[core_models.StateMixin.States.OK, core_models.StateMixin.States.ERRED]) \
            .order_by("-created")
        python_management_requests_serializer = common_serializers.SummaryApplicationSerializer(
            python_management_requests, many=True, context={'select_output': False})
        return python_management_requests_serializer.data

    def find_related_requests(self, jupyter_hub_management):
        jupyter_hub_management_requests = core_managers.SummaryQuerySet(jupyter_hub_management_requests_models).filter(
            jupyter_hub_management=jupyter_hub_management).order_by("-created")
        jupyter_hub_management_requests_serializer = serializers.SummaryJupyterHubManagementRequestsSerializer(
            jupyter_hub_management_requests, many=True, context={'select_output': False})
        return jupyter_hub_management_requests_serializer.data

    @core_mixins.ensure_atomic_transaction
    def perform_create(self, serializer):
        # user cannot create jupyter management if python management has not been created
        jupyter_hub_management = serializer.save()

        self.service.execute_sync_configuration_request_if_allowed(jupyter_hub_management)

        for virtual_env in serializer.validated_data.get('updated_virtual_environments'):
            if virtual_env['jupyter_hub_global']:
                virtual_env_request = models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest(
                    jupyter_hub_management=jupyter_hub_management, virtual_env_name=virtual_env['name'])
                virtual_env_request.save()
                executors.JupyterHubManagementRequestExecutor.execute(virtual_env_request, async=self.async_executor)

    @core_mixins.ensure_atomic_transaction
    def perform_update(self, serializer):
        persisted_jupyter_hub_management = self.get_object()
        incoming_validated_data = serializer.validated_data

        if self.service.has_jupyter_hub_config_changed(incoming_validated_data, persisted_jupyter_hub_management) \
                or self.service.is_last_sync_request_erred(persisted_jupyter_hub_management):
            persisted_jupyter_hub_management = serializer.save()
            self.service.execute_sync_configuration_request_if_allowed(persisted_jupyter_hub_management)

        self.service.issue_localize_globalize_requests(persisted_jupyter_hub_management, serializer.validated_data)

    @core_mixins.ensure_atomic_transaction
    def perform_destroy(self, persisted_jupyter_hub_management):
        self.service.schedule_jupyter_hub_management_removal(persisted_jupyter_hub_management)

    @decorators.detail_route(url_path="requests/(?P<request_uuid>.+)", methods=['get'])
    def find_request_with_output_by_uuid(self, request, uuid=None, request_uuid=None):
        requests = core_managers.SummaryQuerySet(jupyter_hub_management_requests_models).filter(
            jupyter_hub_management=self.get_object(), uuid=request_uuid)
        serializer = serializers.SummaryJupyterHubManagementRequestsSerializer(requests, many=True, context={'select_output': True})
        return response.Response(serializer.data)
