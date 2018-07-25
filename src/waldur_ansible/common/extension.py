from waldur_core.core import WaldurExtension


class AnsibleCommonExtension(WaldurExtension):
    class Settings:
        WALDUR_ANSIBLE_COMMON = {
            'PLAYBOOK_EXECUTION_COMMAND': 'ansible-playbook',
            'PLAYBOOK_ARGUMENTS': ['--verbose'],
            'API_URL': 'https://waldur.example.com/api/',
            'PRIVATE_KEY_PATH': '/etc/waldur/id_rsa',
            'PUBLIC_KEY_UUID': 'Corresponding public key should be stored in the database. Specify here its UUID.',
            'ANSIBLE_REQUEST_TIMEOUT': 3600,
            'ANSIBLE_LIBRARY': '/usr/share/ansible-waldur/',
            'REMOTE_VM_SSH_PORT': '22',
        }

    @staticmethod
    def django_app():
        return 'waldur_ansible.common'

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def get_public_settings():
        return ['ANSIBLE_REQUEST_TIMEOUT', 'PUBLIC_KEY_UUID']
