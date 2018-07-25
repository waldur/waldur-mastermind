from django.utils.functional import cached_property
from waldur_openstack.openstack_tenant.tests import fixtures as openstack_fixtures, factories as openstack_factories

from . import factories


class PythonManagementFixture(openstack_fixtures.OpenStackTenantFixture):
    @cached_property
    def python_management(self):
        instance = self.instance
        internal_ip = openstack_factories.InternalIPFactory(instance=instance, subnet=self.subnet, ip4_address='10.10.10.2')
        internal_ip.save()
        openstack_factories.FloatingIPFactory(address='196.196.220.183', internal_ip=internal_ip)
        return factories.PythonManagementFactory(
            project=self.spl.project,
            virtual_envs_dir_path='my-virtual-envs',
            instance=instance,
            user=self.user,
            system_user='ubuntu',
        )
