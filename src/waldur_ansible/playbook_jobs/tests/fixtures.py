from django.utils.functional import cached_property
from waldur_openstack.openstack_tenant.tests import fixtures as openstack_fixtures

from . import factories


class JobFixture(openstack_fixtures.OpenStackTenantFixture):
    @cached_property
    def job(self):
        return factories.JobFactory(service_project_link=self.spl, subnet=self.subnet)
