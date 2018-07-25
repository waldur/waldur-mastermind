from waldur_ansible.python_management import models
from waldur_core.core import models as core_models


class NullErrorHandler(object):
    def handle_error(self, request, lines_post_processor):
        pass


class DeleteRequestErrorHandler(object):
    def handle_error(self, request, lines_post_processor):
        if not request.python_management.instance or self.exists_failed_initialization_request(request.python_management):
            request.python_management.delete()

    def exists_failed_initialization_request(self, python_management):
        return models.PythonManagementInitializeRequest.objects.get(python_management=python_management).state == core_models.StateMixin.States.ERRED
