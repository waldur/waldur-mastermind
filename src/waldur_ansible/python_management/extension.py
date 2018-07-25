from waldur_ansible.common.extension import AnsibleCommonExtension

from waldur_core.core import WaldurExtension


class PythonManagementExtension(WaldurExtension):
    class Settings:
        WALDUR_PYTHON_MANAGEMENT = {
            'PYTHON_MANAGEMENT_PLAYBOOKS_DIRECTORY': '%swaldur-apps/python_management/' % AnsibleCommonExtension.Settings.WALDUR_ANSIBLE_COMMON['ANSIBLE_LIBRARY'],
            'SYNC_PIP_PACKAGES_TASK_ENABLED': False,
            'SYNC_PIP_PACKAGES_BATCH_SIZE': 300,
        }

    @staticmethod
    def django_app():
        return 'waldur_ansible.python_management'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def celery_tasks():
        from datetime import timedelta
        return {
            'waldur-ansible-sync-pip-packages': {
                'task': 'waldur_ansible.sync_pip_libraries',
                'schedule': timedelta(hours=48),
                'args': (),
            },
        }
