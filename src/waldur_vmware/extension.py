from waldur_core.core import WaldurExtension


class VMwareExtension(WaldurExtension):

    class Settings:
        WALDUR_VMWARE = {
            'BASIC_MODE': False,
        }

    @staticmethod
    def get_public_settings():
        return ['BASIC_MODE']

    @staticmethod
    def django_app():
        return 'waldur_vmware'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in
