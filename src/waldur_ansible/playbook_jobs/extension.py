from waldur_core.core import WaldurExtension


class PlaybookJobsExtension(WaldurExtension):
    class Settings:
        WALDUR_PLAYBOOK_JOBS = {
            'PLAYBOOKS_DIR_NAME': 'ansible_playbooks',
            'PLAYBOOK_ICON_SIZE': (64, 64),
        }

    @staticmethod
    def django_app():
        return 'waldur_ansible.playbook_jobs'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def is_assembly():
        return True
