from waldur_core.core import WaldurExtension


class OpenStackReplicationExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_openstack_replication"

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in
