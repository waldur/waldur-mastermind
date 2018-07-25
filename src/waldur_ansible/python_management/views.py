import logging

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, response
from rest_framework.viewsets import GenericViewSet

from waldur_ansible.common import serializers as common_serializers
from waldur_ansible.jupyter_hub_management import models as jupyter_hub_models

from waldur_core.core import views as core_views, managers as core_managers, mixins as core_mixins
from waldur_core.structure import serializers as core_structure_serializers, filters as structure_filters
from . import models, serializers, executors, pip_service, python_management_service, utils

python_management_requests_models = [models.PythonManagementInitializeRequest,
                                     models.PythonManagementSynchronizeRequest,
                                     models.PythonManagementFindVirtualEnvsRequest,
                                     models.PythonManagementFindInstalledLibrariesRequest,
                                     models.PythonManagementDeleteVirtualEnvRequest,
                                     models.PythonManagementDeleteRequest]

logger = logging.getLogger(__name__)


class PythonManagementViewSet(core_mixins.AsyncExecutor, core_views.ActionsViewSet):
    lookup_field = 'uuid'
    queryset = models.PythonManagement.objects.all().order_by('pk')
    serializer_class = serializers.PythonManagementSerializer
    python_management_request_executor = executors.PythonManagementRequestExecutor
    service = python_management_service.PythonManagementService()

    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)

    def retrieve(self, request, *args, **kwargs):
        python_management = self.get_object()
        python_management_serializer = self.get_serializer(python_management)

        requests = core_managers.SummaryQuerySet(python_management_requests_models).filter(
            python_management=python_management).order_by("-created")
        requests_serializer = common_serializers.SummaryApplicationSerializer(
            requests, many=True, context={'select_output': False})

        return response.Response(
            {'python_management': python_management_serializer.data, 'requests': requests_serializer.data})

    @core_mixins.ensure_atomic_transaction
    def perform_create(self, serializer):
        python_management = serializer.save()

        virtual_environments = serializer.validated_data.get('virtual_environments')

        initialization_request = models.PythonManagementInitializeRequest(python_management=python_management)
        initialization_request.save()

        for virtual_environment in virtual_environments:
            libraries_to_install = []
            for library in virtual_environment['installed_libraries']:
                libraries_to_install.append(library)
            models.PythonManagementSynchronizeRequest.objects.create(
                python_management=python_management,
                initialization_request=initialization_request,
                libraries_to_install=libraries_to_install,
                virtual_env_name=virtual_environment['name'])

        self.python_management_request_executor.execute(initialization_request, async=self.async_executor)

    @core_mixins.ensure_atomic_transaction
    def perform_destroy(self, persisted_python_management):
        self.service.schedule_python_management_removal(persisted_python_management)

    @core_mixins.ensure_atomic_transaction
    def perform_update(self, serializer):
        persisted_python_management = self.get_object()
        serializer.is_valid(raise_exception=True)
        all_transient_virtual_environments = serializer.validated_data.get('virtual_environments')

        self.service.schedule_virtual_environments_update(all_transient_virtual_environments, persisted_python_management)

    @decorators.detail_route(methods=['get'])
    @core_mixins.ensure_atomic_transaction
    def find_virtual_environments(self, request, uuid=None):
        persisted_python_management = self.get_object()

        return self.service.schedule_virtual_environments_search(persisted_python_management)

    @decorators.detail_route(url_path="find_installed_libraries/(?P<virtual_env_name>.+)", methods=['get'])
    @core_mixins.ensure_atomic_transaction
    def find_installed_libraries(self, request, virtual_env_name=None, uuid=None):
        persisted_python_management = self.get_object()

        return self.service.schedule_installed_libraries_search(persisted_python_management, virtual_env_name)

    @decorators.detail_route(url_path="requests/(?P<request_uuid>.+)", methods=['get'])
    def find_request_with_output_by_uuid(self, request, uuid=None, request_uuid=None):
        requests = core_managers.SummaryQuerySet(python_management_requests_models).filter(python_management=self.get_object(),
                                                                                           uuid=request_uuid)
        serializer = common_serializers.SummaryApplicationSerializer(
            requests, many=True, context={'select_output': True})
        return response.Response(serializer.data)

    @decorators.list_route(url_path="validForJupyterHub", methods=['get'])
    def find_valid_for_jupyter_hub_python_managements_with_instance_info(self, request):
        result = super(PythonManagementViewSet, self).list(request)
        result.data = filter(
            lambda pm: not utils.execute_safely(
                lambda: jupyter_hub_models.JupyterHubManagement.objects.get(python_management__uuid=pm['uuid'])),
            result.data)

        for python_management in result.data:
            instance = models.PythonManagement.objects.get(uuid=python_management['uuid']).instance
            instance_serializer = core_structure_serializers.SummaryResourceSerializer(instance=instance, context={'request': request})
            python_management['instance'] = instance_serializer.data

        return result


class PipPackagesViewSet(GenericViewSet):

    @decorators.list_route(url_path="find_library_versions/(?P<queried_library_name>.+)/(?P<python_version>.+)", methods=['get'])
    def find_library_versions(self, request, queried_library_name=None, python_version=None):
        versions = pip_service.find_versions(queried_library_name, python_version)

        return response.Response({'versions': versions})

    @decorators.list_route(url_path="autocomplete_library/(?P<queried_library_name>.+)", methods=['get'])
    def autocomplete_library_name(self, request, queried_library_name=None):
        matching_libraries = pip_service.autocomplete_library_name(queried_library_name)
        serializer = serializers.CachedRepositoryPythonLibrarySerializer(matching_libraries, many=True)

        return response.Response(serializer.data)
