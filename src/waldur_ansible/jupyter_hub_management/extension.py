from waldur_ansible.common.extension import AnsibleCommonExtension

from waldur_core.core import WaldurExtension


class JupyterHubManagementExtension(WaldurExtension):
    class Settings:
        WALDUR_JUPYTER_HUB_MANAGEMENT = {
            'JUPYTER_MANAGEMENT_PLAYBOOKS_DIRECTORY': '%swaldur-apps/jupyter_hub_management/' % AnsibleCommonExtension.Settings.WALDUR_ANSIBLE_COMMON['ANSIBLE_LIBRARY'],
        }

    @staticmethod
    def django_app():
        return 'waldur_ansible.jupyter_hub_management'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in
