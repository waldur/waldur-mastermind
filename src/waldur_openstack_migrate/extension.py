from waldur_core.core import WaldurExtension


class OpenStackMigrateExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_openstack_migrate"

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
