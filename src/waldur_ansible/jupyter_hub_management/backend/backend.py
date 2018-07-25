import logging

from django.conf import settings
from waldur_ansible.common import backend as common_backend
from waldur_ansible.jupyter_hub_management import models, constants
from waldur_ansible.jupyter_hub_management.backend import locking_service
from waldur_ansible.python_management.backend import extracted_information_handlers as python_handlers, output_lines_post_processors as python_post_processors, error_handlers as python_error_handlers

from . import additional_extra_args_builders, extracted_information_handlers, error_handlers

logger = logging.getLogger(__name__)


class JupyterHubManagementBackend(common_backend.ManagementRequestsBackend):
    REQUEST_TYPES_PLAYBOOKS_MAP = {
        models.JupyterHubManagementSyncConfigurationRequest: constants.JupyterHubManagementConstants.SYNC_CONFIGURATION,
        models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: constants.JupyterHubManagementConstants.MAKE_VIRTUAL_ENV_GLOBAL,
        models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: constants.JupyterHubManagementConstants.MAKE_VIRTUAL_ENV_LOCAL,
        models.JupyterHubManagementDeleteRequest: constants.JupyterHubManagementConstants.DELETE_JUPYTER_HUB,
    }

    REQUEST_TYPES_EXTRA_ARGS_MAP = {
        models.JupyterHubManagementSyncConfigurationRequest: additional_extra_args_builders.build_sync_config_extra_args,
        models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: additional_extra_args_builders.build_virtual_env_extra_args,
        models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: additional_extra_args_builders.build_virtual_env_extra_args,
        models.JupyterHubManagementDeleteRequest: additional_extra_args_builders.build_delete_jupyter_hub_extra_args,
    }

    REQUEST_TYPES_POST_PROCESSOR_MAP = {
        models.JupyterHubManagementSyncConfigurationRequest: python_post_processors.NullOutputLinesPostProcessor,
        models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: python_post_processors.InstalledLibrariesOutputLinesPostProcessor,
        models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: python_post_processors.InstalledLibrariesOutputLinesPostProcessor,
        models.JupyterHubManagementDeleteRequest: python_post_processors.NullOutputLinesPostProcessor,
    }

    REQUEST_TYPES_HANDLERS_MAP = {
        models.JupyterHubManagementSyncConfigurationRequest: python_handlers.NullExtractedInformationHandler,
        models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: extracted_information_handlers.JupyterHubVirtualEnvironmentGlobalExtractedInformationHandler,
        models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: extracted_information_handlers.JupyterHubVirtualEnvironmentLocalExtractedInformationHandler,
        models.JupyterHubManagementDeleteRequest: extracted_information_handlers.JupyterHubManagementDeleteExtractedInformationHandler,
    }

    REQUEST_TYPES_ERROR_HANDLERS_CORRESPONDENCE = {
        models.JupyterHubManagementSyncConfigurationRequest: python_error_handlers.NullErrorHandler,
        models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest: python_error_handlers.NullErrorHandler,
        models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest: python_error_handlers.NullErrorHandler,
        models.JupyterHubManagementDeleteRequest: error_handlers.DeleteRequestErrorHandler,
    }

    LOCKED_FOR_PROCESSING = 'Whole environment or the particular virtual environment ' \
                            'is now being processed, request cannot be executed!'

    def process_jupyter_hub_management_request(self, jupyter_hub_management_request):
        self.process_request(jupyter_hub_management_request)

    def is_processing_allowed(self, request):
        return locking_service.JupyterHubManagementBackendLockingService.is_processing_allowed(request)

    def build_locked_for_processing_message(self, request):
        return JupyterHubManagementBackend.LOCKED_FOR_PROCESSING

    def lock_for_processing(self, request):
        locking_service.JupyterHubManagementBackendLockingService.lock_for_processing(request)

    def handle_on_processing_finished(self, request):
        locking_service.JupyterHubManagementBackendLockingService.handle_on_processing_finished(request)

    def get_playbook_path(self, request):
        return settings.WALDUR_JUPYTER_HUB_MANAGEMENT.get('JUPYTER_MANAGEMENT_PLAYBOOKS_DIRECTORY') \
            + JupyterHubManagementBackend.REQUEST_TYPES_PLAYBOOKS_MAP.get(type(request)) \
            + '.yml'

    def get_user(self, request):
        return request.jupyter_hub_management.python_management.user

    def build_additional_extra_vars(self, request):
        python_management = request.jupyter_hub_management.python_management
        extra_vars = dict(
            instance_public_ip=python_management.instance.external_ips[0] if python_management.instance else None,
            virtual_envs_dir_path=python_management.virtual_envs_dir_path,
            default_system_user=python_management.system_user,
        )

        additional_extra_args_building_function = JupyterHubManagementBackend.REQUEST_TYPES_EXTRA_ARGS_MAP.get(type(request))
        if additional_extra_args_building_function:
            extra_vars.update(additional_extra_args_building_function(request))

        return extra_vars

    def instantiate_line_post_processor_class(self, request):
        lines_post_processor_class = JupyterHubManagementBackend.REQUEST_TYPES_POST_PROCESSOR_MAP.get(type(request))
        return lines_post_processor_class()

    def instantiate_extracted_information_handler_class(self, request):
        extracted_information_handler_class = JupyterHubManagementBackend.REQUEST_TYPES_HANDLERS_MAP.get(type(request))
        return extracted_information_handler_class()

    def instantiate_error_handler_class(self, request):
        extracted_information_handler_class = JupyterHubManagementBackend.REQUEST_TYPES_ERROR_HANDLERS_CORRESPONDENCE.get(type(request))
        return extracted_information_handler_class()
