from rest_framework.exceptions import APIException
from rest_framework.status import HTTP_423_LOCKED
from waldur_ansible.jupyter_hub_management.backend import locking_service
from waldur_ansible.python_management import models as python_management_models, utils as python_management_utils

from waldur_core.core import models as core_models
from . import models, executors


class JupyterHubManagementService(object):
    executor = executors.JupyterHubManagementRequestExecutor

    def schedule_jupyter_hub_management_removal(self, persisted_jupyter_hub_management):
        delete_request = models.JupyterHubManagementDeleteRequest(jupyter_hub_management=persisted_jupyter_hub_management)

        if not locking_service.JupyterHubManagementBackendLockingService.is_processing_allowed(delete_request):
            raise APIException(code=HTTP_423_LOCKED)

        delete_request.save()

        self.executor.execute(delete_request, async=True)

    def issue_localize_globalize_requests(self, updated_jupyter_hub_management, validated_data):
        virtual_environments = validated_data['updated_virtual_environments']
        virtual_environments_to_localize = []
        virtual_environments_to_globalize = []
        for virtual_environment in virtual_environments:
            persisted_virtual_environment = python_management_models.VirtualEnvironment.objects.get(
                python_management=updated_jupyter_hub_management.python_management, name=virtual_environment['name'])
            if persisted_virtual_environment.jupyter_hub_global is not virtual_environment['jupyter_hub_global']:
                if virtual_environment['jupyter_hub_global']:
                    virtual_environments_to_globalize.append(virtual_environment)
                else:
                    virtual_environments_to_localize.append(virtual_environment)

        for virtual_environment_to_globalize in virtual_environments_to_globalize:
            globalize_request = models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest(
                jupyter_hub_management=updated_jupyter_hub_management, virtual_env_name=virtual_environment_to_globalize['name'])
            self.execute_or_refuse_request(globalize_request)

        for virtual_environment_to_localize in virtual_environments_to_localize:
            localize_request = models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest(
                jupyter_hub_management=updated_jupyter_hub_management, virtual_env_name=virtual_environment_to_localize['name'])
            self.execute_or_refuse_request(localize_request)

    def execute_or_refuse_request(self, localize_request):
        if locking_service.JupyterHubManagementBackendLockingService.is_processing_allowed(localize_request):
            localize_request.save()
            self.executor.execute(localize_request, async=True)

    def execute_sync_configuration_request_if_allowed(self, persisted_jupyter_hub_management):
        sync_config_request = models.JupyterHubManagementSyncConfigurationRequest(jupyter_hub_management=persisted_jupyter_hub_management)
        if not locking_service.JupyterHubManagementBackendLockingService.is_processing_allowed(sync_config_request):
            raise APIException(code=HTTP_423_LOCKED)
        sync_config_request.save()
        self.executor.execute(sync_config_request, async=True)

    def has_jupyter_hub_config_changed(self, incoming_validated_data, persisted_jupyter_hub_management):
        removed_jupyter_hub_users = self.find_removed_users(persisted_jupyter_hub_management.jupyter_hub_users.all(), incoming_validated_data.get('jupyter_hub_users'))
        if removed_jupyter_hub_users:
            return True
        for jupyter_hub_user in incoming_validated_data.get('jupyter_hub_users'):
            persisted_jupyter_hub_user = self.find_corresponding_persisted_jupyter_hub_user(jupyter_hub_user['username'], persisted_jupyter_hub_management)
            if persisted_jupyter_hub_user is None \
                    or persisted_jupyter_hub_user.admin != jupyter_hub_user['admin'] \
                    or jupyter_hub_user['password'] \
                    or persisted_jupyter_hub_user.whitelisted != jupyter_hub_user['whitelisted']:
                return True
        root_model_changed = incoming_validated_data.get('session_time_to_live_hours') != persisted_jupyter_hub_management.session_time_to_live_hours
        if not root_model_changed:
            persisted_oauth_config = persisted_jupyter_hub_management.jupyter_hub_oauth_config
            if persisted_oauth_config:
                incoming_oauth_config = incoming_validated_data.get('jupyter_hub_oauth_config')
                return persisted_oauth_config.type != incoming_oauth_config.get('type') \
                    or persisted_oauth_config.oauth_callback_url != incoming_oauth_config.get('oauth_callback_url') \
                    or persisted_oauth_config.client_id != incoming_oauth_config.get('client_id') \
                    or persisted_oauth_config.client_secret != incoming_oauth_config.get('client_secret') \
                    or persisted_oauth_config.tenant_id != incoming_oauth_config.get('tenant_id') \
                    or persisted_oauth_config.gitlab_host != incoming_oauth_config.get('gitlab_host')
        else:
            return True

    def find_removed_users(self, persisted_jupyter_hub_users, jupyter_hub_users):
        result = []
        for persisted_jupyter_hub_user in persisted_jupyter_hub_users:
            if not filter(lambda u: u['username'] == persisted_jupyter_hub_user.username, jupyter_hub_users):
                result.append(persisted_jupyter_hub_user)
        return result

    def find_corresponding_persisted_jupyter_hub_user(self, username, jupyter_hub_management):
        return python_management_utils.execute_safely(lambda: models.JupyterHubUser.objects.get(username=username, jupyter_hub_management=jupyter_hub_management))

    def is_last_sync_request_erred(self, persisted_jupyter_hub_management):
        last_sync_config_request = python_management_utils.execute_safely(
            lambda: models.JupyterHubManagementSyncConfigurationRequest.objects.filter(jupyter_hub_management=persisted_jupyter_hub_management).latest('id'))
        return last_sync_config_request.state == core_models.StateMixin.States.ERRED if last_sync_config_request else True
