from waldur_ansible.python_management import models as python_management_models, utils as python_management_utils
from waldur_ansible.python_management.backend import extracted_information_handlers as python_management_handlers


def set_affected_virtual_env_global_or_not(request, jupyter_hub_global):
    jupyter_hub_management = request.jupyter_hub_management
    affected_virtual_env = python_management_utils.execute_safely(
        lambda: python_management_models.VirtualEnvironment.objects.get(python_management=jupyter_hub_management.python_management, name=request.virtual_env_name))
    if affected_virtual_env:
        affected_virtual_env.jupyter_hub_global = jupyter_hub_global
        affected_virtual_env.save()


def update_installed_libraries_list(lines_post_processor, request):
    installed_libraries_handler = python_management_handlers.InstalledLibrariesExtractedInformationHandler()
    installed_libraries_handler.persist_installed_libraries_in_db(
        request.jupyter_hub_management.python_management, request.virtual_env_name, lines_post_processor.installed_libraries_after_modifications)


class JupyterHubVirtualEnvironmentGlobalExtractedInformationHandler(object):

    def handle_extracted_information(self, request, lines_post_processor):
        set_affected_virtual_env_global_or_not(request, True)
        update_installed_libraries_list(lines_post_processor, request)


class JupyterHubVirtualEnvironmentLocalExtractedInformationHandler(object):
    def handle_extracted_information(self, request, lines_post_processor):
        set_affected_virtual_env_global_or_not(request, False)
        update_installed_libraries_list(lines_post_processor, request)


class JupyterHubManagementDeleteExtractedInformationHandler(object):
    def handle_extracted_information(self, request, lines_post_processor):
        for global_virtual_env in request.jupyter_hub_management.python_management.virtual_environments.filter(jupyter_hub_global=True):
            global_virtual_env.jupyter_hub_global = False
            global_virtual_env.save()
        request.jupyter_hub_management.delete()
