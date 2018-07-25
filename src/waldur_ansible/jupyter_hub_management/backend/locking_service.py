from django.conf import settings
from waldur_ansible.common import cache_utils as python_cache_utils
from waldur_ansible.jupyter_hub_management import models
from waldur_ansible.python_management.backend import locking_service as python_locking_service
from waldur_ansible.python_management.backend.locking_service import PythonManagementBackendLockBuilder

JUPYTOR_HUB_MANAGEMENT_LOCK = 'waldur_jupyter_hub_management_global_%s'


class JupyterHubConfigRequestProcessingFinishedLockingHandler(object):
    def handle_on_processing_finished(self, request):
        python_cache_utils.release_task_status(
            JupyterHubManagementBackendLockBuilder.build_global_lock(request.jupyter_hub_management.python_management))


class JupyterHubConfigRelatedToVirtualEnvRequestProcessingFinishedLockingHandler(object):
    def handle_on_processing_finished(self, request):
        python_cache_utils.release_task_status(
            python_locking_service.PythonManagementBackendLockBuilder.build_related_to_virt_env_lock(
                request.jupyter_hub_management.python_management, request.virtual_env_name))


class JupyterHubConfigProcessingAllowedDecider(object):
    def is_processing_allowed(self, request):
        global_lock = JupyterHubManagementBackendLockBuilder.build_global_lock(request.jupyter_hub_management.python_management)
        return not python_cache_utils.is_syncing(global_lock)


class JupyterHubRelatedToVirtualEnvProcessingAllowedDecider(object):
    def is_processing_allowed(self, request):
        virtual_env_lock = python_locking_service.PythonManagementBackendLockBuilder.build_related_to_virt_env_lock(
            request.jupyter_hub_management.python_management, request.virtual_env_name)
        global_lock = python_locking_service.PythonManagementBackendLockBuilder.build_global_lock(request.jupyter_hub_management.python_management)
        return not python_cache_utils.is_syncing(virtual_env_lock) and not python_cache_utils.is_syncing(global_lock)


class JupyterHubConfigSynchronizer(object):
    def lock(self, request):
        global_lock = JupyterHubManagementBackendLockBuilder.build_global_lock(request.jupyter_hub_management.python_management)
        python_cache_utils.renew_task_status(global_lock, settings.WALDUR_ANSIBLE_COMMON['ANSIBLE_REQUEST_TIMEOUT'])


class JupyterHubRelatedToVirtualEnvSynchronizer(object):
    def lock(self, request):
        virtual_env_lock = PythonManagementBackendLockBuilder.build_related_to_virt_env_lock(
            request.jupyter_hub_management.python_management, request.virtual_env_name)
        python_cache_utils.renew_task_status(virtual_env_lock, settings.WALDUR_ANSIBLE_COMMON['ANSIBLE_REQUEST_TIMEOUT'])


class JupyterHubManagementBackendLockingService(object):

    @staticmethod
    def lock_for_processing(request):
        synchronizer = JupyterHubManagementBackendLockBuilder.intantiate_synchronizer(type(request))
        return synchronizer.lock(request)

    @staticmethod
    def is_processing_allowed(request):
        processing_allowed_decider = JupyterHubManagementBackendLockBuilder.intantiate_processing_allowed_decider(type(request))
        return processing_allowed_decider.is_processing_allowed(request)

    @staticmethod
    def handle_on_processing_finished(request):
        locking_handler = JupyterHubManagementBackendLockBuilder.intantiate_processing_finished_locking_handler(type(request))
        locking_handler.handle_on_processing_finished(request)


class JupyterHubManagementBackendLockBuilder(object):
    REQUEST_TYPES_PROCESSING_ALLOWED_DECIDER = {
        models.JupyterHubManagementSyncConfigurationRequest: JupyterHubConfigProcessingAllowedDecider,
        models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: JupyterHubRelatedToVirtualEnvProcessingAllowedDecider,
        models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: JupyterHubRelatedToVirtualEnvProcessingAllowedDecider,
        models.JupyterHubManagementDeleteRequest: JupyterHubConfigProcessingAllowedDecider,
    }
    REQUEST_TYPES_SYNCHRONIZERS = {
        models.JupyterHubManagementSyncConfigurationRequest: JupyterHubConfigSynchronizer,
        models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: JupyterHubRelatedToVirtualEnvSynchronizer,
        models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: JupyterHubRelatedToVirtualEnvSynchronizer,
        models.JupyterHubManagementDeleteRequest: JupyterHubConfigSynchronizer,
    }
    REQUEST_TYPES_PROCESSING_FINISHED_LOCKING_HANDLER = {
        models.JupyterHubManagementSyncConfigurationRequest: JupyterHubConfigRequestProcessingFinishedLockingHandler,
        models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: JupyterHubConfigRelatedToVirtualEnvRequestProcessingFinishedLockingHandler,
        models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: JupyterHubConfigRelatedToVirtualEnvRequestProcessingFinishedLockingHandler,
        models.JupyterHubManagementDeleteRequest: JupyterHubConfigRequestProcessingFinishedLockingHandler,
    }

    @staticmethod
    def intantiate_processing_allowed_decider(jupyter_hub_management_request_class):
        processing_allowed_decider_class = JupyterHubManagementBackendLockBuilder.REQUEST_TYPES_PROCESSING_ALLOWED_DECIDER \
            .get(jupyter_hub_management_request_class)
        return processing_allowed_decider_class()

    @staticmethod
    def intantiate_processing_finished_locking_handler(jupyter_hub_management_request_class):
        locking_handler_class = JupyterHubManagementBackendLockBuilder.REQUEST_TYPES_PROCESSING_FINISHED_LOCKING_HANDLER \
            .get(jupyter_hub_management_request_class)
        return locking_handler_class()

    @staticmethod
    def intantiate_synchronizer(jupyter_hub_management_request_class):
        locking_handler_class = JupyterHubManagementBackendLockBuilder.REQUEST_TYPES_SYNCHRONIZERS \
            .get(jupyter_hub_management_request_class)
        return locking_handler_class()

    @staticmethod
    def build_global_lock(persisted_jupyter_hub_management):
        return JUPYTOR_HUB_MANAGEMENT_LOCK % persisted_jupyter_hub_management.pk
