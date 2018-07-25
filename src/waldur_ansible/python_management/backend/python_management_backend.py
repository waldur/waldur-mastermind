import logging

from django.conf import settings
from waldur_ansible.common import backend as common_backend
from waldur_ansible.python_management import models, constants, executors

from . import output_lines_post_processors, locking_service, extracted_information_handlers, additional_extra_args_builders, error_handlers

logger = logging.getLogger(__name__)


class PythonManagementBackend(common_backend.ManagementRequestsBackend):
    REQUEST_TYPES_PLAYBOOKS_CORRESPONDENCE = {
        models.PythonManagementInitializeRequest: constants.PythonManagementConstants.INSTALL_PYTHON_ENVIRONMENT,
        models.PythonManagementSynchronizeRequest: constants.PythonManagementConstants.SYNCHRONIZE_PACKAGES,
        models.PythonManagementFindVirtualEnvsRequest: constants.PythonManagementConstants.FIND_INSTALLED_VIRTUAL_ENVIRONMENTS,
        models.PythonManagementFindInstalledLibrariesRequest: constants.PythonManagementConstants.FIND_INSTALLED_LIBRARIES_FOR_VIRTUAL_ENVIRONMENT,
        models.PythonManagementDeleteVirtualEnvRequest: constants.PythonManagementConstants.DELETE_VIRTUAL_ENVIRONMENT,
        models.PythonManagementDeleteRequest: constants.PythonManagementConstants.DELETE_PYTHON_ENVIRONMENT,
    }

    REQUEST_TYPES_EXTRA_ARGS_CORRESPONDENCE = {
        models.PythonManagementInitializeRequest: None,
        models.PythonManagementSynchronizeRequest: additional_extra_args_builders.build_sync_request_extra_args,
        models.PythonManagementFindVirtualEnvsRequest: None,
        models.PythonManagementFindInstalledLibrariesRequest: additional_extra_args_builders.build_additional_extra_args,
        models.PythonManagementDeleteVirtualEnvRequest: additional_extra_args_builders.build_additional_extra_args,
        models.PythonManagementDeleteRequest: None,
    }

    REQUEST_TYPES_POST_PROCESSOR_CORRESPONDENCE = {
        models.PythonManagementInitializeRequest: output_lines_post_processors.InitializationOutputLinesPostProcessor,
        models.PythonManagementSynchronizeRequest: output_lines_post_processors.InstalledLibrariesOutputLinesPostProcessor,
        models.PythonManagementFindVirtualEnvsRequest: output_lines_post_processors.InstalledVirtualEnvironmentsOutputLinesPostProcessor,
        models.PythonManagementFindInstalledLibrariesRequest: output_lines_post_processors.InstalledLibrariesOutputLinesPostProcessor,
        models.PythonManagementDeleteVirtualEnvRequest: output_lines_post_processors.NullOutputLinesPostProcessor,
        models.PythonManagementDeleteRequest: output_lines_post_processors.NullOutputLinesPostProcessor,
    }

    REQUEST_TYPES_HANDLERS_CORRESPONDENCE = {
        models.PythonManagementInitializeRequest: extracted_information_handlers.InitializationRequestExtractedInformationHandler,
        models.PythonManagementSynchronizeRequest: extracted_information_handlers.InstalledLibrariesExtractedInformationHandler,
        models.PythonManagementFindVirtualEnvsRequest: extracted_information_handlers.PythonManagementFindVirtualEnvsRequestExtractedInformationHandler,
        models.PythonManagementFindInstalledLibrariesRequest: extracted_information_handlers.InstalledLibrariesExtractedInformationHandler,
        models.PythonManagementDeleteVirtualEnvRequest: extracted_information_handlers.PythonManagementDeleteVirtualEnvExtractedInformationHandler,
        models.PythonManagementDeleteRequest: extracted_information_handlers.PythonManagementDeletionRequestExtractedInformationHandler,
    }

    REQUEST_TYPES_ERROR_HANDLERS_CORRESPONDENCE = {
        models.PythonManagementInitializeRequest: error_handlers.NullErrorHandler,
        models.PythonManagementSynchronizeRequest: error_handlers.NullErrorHandler,
        models.PythonManagementFindVirtualEnvsRequest: error_handlers.NullErrorHandler,
        models.PythonManagementFindInstalledLibrariesRequest: error_handlers.NullErrorHandler,
        models.PythonManagementDeleteVirtualEnvRequest: error_handlers.NullErrorHandler,
        models.PythonManagementDeleteRequest: error_handlers.DeleteRequestErrorHandler,
    }

    LOCKED_FOR_PROCESSING = 'Whole environment or the particular virutal environnment is now being processed, request cannot be executed!'

    def process_python_management_request(self, python_management_request):
        self.process_request(python_management_request)

    def is_processing_allowed(self, request):
        return locking_service.PythonManagementBackendLockingService.is_processing_allowed(request)

    def build_locked_for_processing_message(self, request):
        return PythonManagementBackend.LOCKED_FOR_PROCESSING

    def lock_for_processing(self, request):
        locking_service.PythonManagementBackendLockingService.lock_for_processing(request)

    def handle_on_processing_finished(self, request):
        locking_service.PythonManagementBackendLockingService.handle_on_processing_finished(request)

    def get_playbook_path(self, request):
        return settings.WALDUR_PYTHON_MANAGEMENT.get('PYTHON_MANAGEMENT_PLAYBOOKS_DIRECTORY') \
            + PythonManagementBackend.REQUEST_TYPES_PLAYBOOKS_CORRESPONDENCE.get(type(request)) \
            + '.yml'

    def get_user(self, request):
        return request.python_management.user

    def build_additional_extra_vars(self, request):
        python_management = request.python_management
        extra_vars = dict(
            instance_public_ip=python_management.instance.external_ips[0] if python_management.instance else None,
            virtual_envs_dir_path=python_management.virtual_envs_dir_path,
            default_system_user=python_management.system_user,
        )

        additional_extra_args_building_function = PythonManagementBackend.REQUEST_TYPES_EXTRA_ARGS_CORRESPONDENCE.get(type(request))
        if additional_extra_args_building_function:
            extra_vars.update(additional_extra_args_building_function(request))

        return extra_vars

    def instantiate_line_post_processor_class(self, request):
        lines_post_processor_class = PythonManagementBackend.REQUEST_TYPES_POST_PROCESSOR_CORRESPONDENCE \
            .get(type(request))
        return lines_post_processor_class()

    def instantiate_extracted_information_handler_class(self, request):
        extracted_information_handler_class = PythonManagementBackend.REQUEST_TYPES_HANDLERS_CORRESPONDENCE \
            .get(type(request))
        return extracted_information_handler_class()

    def instantiate_error_handler_class(self, request):
        extracted_information_handler_class = PythonManagementBackend.REQUEST_TYPES_ERROR_HANDLERS_CORRESPONDENCE.get(type(request))
        return extracted_information_handler_class()


class PythonManagementInitializationBackend(PythonManagementBackend):

    def process_python_management_request(self, python_management_initialization_request):
        super(PythonManagementInitializationBackend, self).process_python_management_request(python_management_initialization_request)

        for synchronization_request in python_management_initialization_request.sychronization_requests.all():
            executors.PythonManagementRequestExecutor.execute(synchronization_request, async=True)
