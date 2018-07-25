from waldur_ansible.jupyter_hub_management import models
from waldur_core.core import models as core_models


class DeleteRequestErrorHandler(object):
    def handle_error(self, request, lines_post_processor):
        if not request.jupyter_hub_management.instance or self.exists_failed_initialization_request(request.jupyter_hub_management):
            request.jupyter_hub_management.delete()

    def exists_failed_initialization_request(self, jupyter_hub_management):
        return models.JupyterHubManagementSyncConfigurationRequest.objects.filter(jupyter_hub_management=jupyter_hub_management).latest('id').state \
            == core_models.StateMixin.States.ERRED
