from waldur_core.core import WaldurExtension


class OpenStackTenantExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_openstack_tenant"
