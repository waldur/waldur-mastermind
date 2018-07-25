from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.status import HTTP_202_ACCEPTED, HTTP_423_LOCKED, HTTP_412_PRECONDITION_FAILED
from waldur_ansible.common import cache_utils
from waldur_ansible.python_management.backend import locking_service

from waldur_core.core.models import StateMixin
from . import models, executors


class PythonManagementService(object):
    executor = executors.PythonManagementRequestExecutor

    @staticmethod
    def build_response(locked_virtual_envs):
        return {
            'locked': 'Python management is locked, please retry later',
            'global_lock': not bool(locked_virtual_envs),
            'locked_virtual_envs': locked_virtual_envs,
        }

    @staticmethod
    def build_python_management_locked_response(locked_virtual_envs=None):
        if not locked_virtual_envs:
            locked_virtual_envs = []
        return Response(
            PythonManagementService.build_response(locked_virtual_envs),
            status=HTTP_423_LOCKED)

    def schedule_python_management_removal(self, persisted_python_management):
        if persisted_python_management.jupyter_hub_management.all():
            raise APIException(code=HTTP_412_PRECONDITION_FAILED)
        delete_request = models.PythonManagementDeleteRequest(python_management=persisted_python_management)

        if not locking_service.PythonManagementBackendLockingService.is_processing_allowed(delete_request):
            raise APIException(code=HTTP_423_LOCKED)

        delete_request.save()
        self.executor.execute(delete_request, async=True)

    def schedule_virtual_environments_search(self, persisted_python_management):
        find_virtual_envs_request = models.PythonManagementFindVirtualEnvsRequest(python_management=persisted_python_management)

        if not locking_service.PythonManagementBackendLockingService.is_processing_allowed(find_virtual_envs_request):
            raise APIException(code=HTTP_423_LOCKED)

        find_virtual_envs_request.save()
        self.executor.execute(find_virtual_envs_request, async=True)
        return Response({'status': 'Find installed virtual environments process has been scheduled.'},
                        status=HTTP_202_ACCEPTED)

    def schedule_installed_libraries_search(self, persisted_python_management, virtual_env_name):
        find_installed_libraries_request = models.PythonManagementFindInstalledLibrariesRequest(
            python_management=persisted_python_management, virtual_env_name=virtual_env_name)

        if not locking_service.PythonManagementBackendLockingService.is_processing_allowed(find_installed_libraries_request):
            raise APIException(code=HTTP_423_LOCKED)

        find_installed_libraries_request.save()
        self.executor.execute(find_installed_libraries_request, async=True)
        return Response(
            {'status': 'Find installed libraries in virtual environment process has been scheduled.'},
            status=HTTP_202_ACCEPTED)

    def schedule_virtual_environments_update(self, all_transient_virtual_environments, persisted_python_management):
        persisted_virtual_environments = persisted_python_management.virtual_environments.all()
        virtual_environments_to_create, virtual_environments_to_change, removed_virtual_environments = \
            self.identify_changed_created_removed_envs(
                all_transient_virtual_environments, persisted_virtual_environments)

        if cache_utils.is_syncing(
                locking_service.PythonManagementBackendLockBuilder.build_global_lock(persisted_python_management)):
            raise APIException(code=HTTP_423_LOCKED)

        self.create_or_refuse_requests(
            persisted_python_management, removed_virtual_environments,
            virtual_environments_to_change, virtual_environments_to_create)

    def identify_changed_created_removed_envs(self, all_transient_virtual_environments, persisted_virtual_environments):

        removed_virtual_environments = []
        virtual_environments_to_create = []
        virtual_environments_to_change = []

        for virtual_environment in persisted_virtual_environments:
            libraries_to_remove = []
            libraries_to_install = []
            virtual_environment_name = virtual_environment.name
            corresponding_transient_virtual_environment = self.find_corresponding_transient_virtual_environment(
                virtual_environment_name, all_transient_virtual_environments)
            if not corresponding_transient_virtual_environment:
                removed_virtual_environments.append(virtual_environment)
            else:
                transient_libraries = corresponding_transient_virtual_environment['installed_libraries']
                persisted_libraries = virtual_environment.installed_libraries.all()

                for installed_library in persisted_libraries:
                    transient_library = self.find_corresponding_transient_library(
                        installed_library, transient_libraries)
                    if not transient_library:
                        libraries_to_remove.append(
                            {'name': installed_library.name, 'version': installed_library.version})

                for transient_library in transient_libraries:
                    persisted_library = self.find_corresponding_persisted_library(
                        transient_library, persisted_libraries)

                    if not persisted_library:
                        libraries_to_install.append(transient_library)

                if libraries_to_remove or libraries_to_install:
                    virtual_environments_to_change.append({
                        'name': virtual_environment_name,
                        'libraries_to_install': libraries_to_install,
                        'libraries_to_remove': libraries_to_remove})

        for transient_virtual_environment in all_transient_virtual_environments:
            persisted_virtual_environment = self.find_corresponding_persisted_virtual_environment(
                transient_virtual_environment['name'], persisted_virtual_environments)
            if not persisted_virtual_environment:
                virtual_environments_to_create.append(transient_virtual_environment)

        return virtual_environments_to_create, virtual_environments_to_change, removed_virtual_environments

    def is_global_request(self, request):
        return not request.virtual_env_name

    def is_request_executing(self, request):
        return request.state != StateMixin.States.OK and request.state != StateMixin.States.ERRED

    def find_corresponding_persisted_library(self, transient_library, persisted_libraries):
        for persisted_library in persisted_libraries:
            if persisted_library.name == transient_library['name'] and persisted_library.version == transient_library['version']:
                return persisted_library
        return None

    def find_corresponding_transient_library(self, persisted_library, transient_libraries):
        for transient_library in transient_libraries:
            if transient_library['name'] == persisted_library.name and transient_library['version'] == persisted_library.version:
                return transient_library
        return None

    def find_corresponding_persisted_virtual_environment(self, virtual_environment_name, persisted_virtual_environments):
        for persisted_virtual_environment in persisted_virtual_environments:
            if persisted_virtual_environment.name == virtual_environment_name:
                return persisted_virtual_environment
        return None

    def find_corresponding_transient_virtual_environment(self, virtual_environment_name,
                                                         transient_virtual_environments):
        for transient_virtual_environment in transient_virtual_environments:
            if transient_virtual_environment['name'] == virtual_environment_name:
                return transient_virtual_environment
        return None

    def create_or_refuse_requests(self, persisted_python_management, removed_virtual_environments,
                                  virtual_environments_to_change, virtual_environments_to_create):
        for virtual_environment_to_create in virtual_environments_to_create:
            sync_request = models.PythonManagementSynchronizeRequest(
                python_management=persisted_python_management,
                libraries_to_install=virtual_environment_to_create['installed_libraries'],
                virtual_env_name=virtual_environment_to_create['name'])

            self.create_or_refuse_request(sync_request)

        for removed_virtual_environment in removed_virtual_environments:
            delete_virt_env_request = models.PythonManagementDeleteVirtualEnvRequest(
                python_management=persisted_python_management,
                virtual_env_name=removed_virtual_environment.name)

            self.create_or_refuse_request(delete_virt_env_request)

        for virtual_environment_to_change in virtual_environments_to_change:
            sync_request = models.PythonManagementSynchronizeRequest(
                python_management=persisted_python_management,
                libraries_to_install=virtual_environment_to_change['libraries_to_install'],
                libraries_to_remove=virtual_environment_to_change['libraries_to_remove'],
                virtual_env_name=virtual_environment_to_change['name'])

            self.create_or_refuse_request(sync_request)

    def create_or_refuse_request(self, sync_request):
        if locking_service.PythonManagementBackendLockingService.is_processing_allowed(sync_request):
            sync_request.save()
            self.executor.execute(sync_request, async=True)
