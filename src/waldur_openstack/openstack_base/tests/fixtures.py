from django.utils.functional import cached_property

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_openstack.openstack import apps as openstack_apps
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests.factories import TenantFactory


class OpenStackFixture(ProjectFixture):
    @cached_property
    def openstack_service_settings(self):
        # OpenStack packages should be used only with shared settings.
        return structure_factories.ServiceSettingsFactory(
            type=openstack_apps.OpenStackConfig.service_name,
            shared=True,
            options={'external_network_id': 'test_network_id'},
            state=structure_models.ServiceSettings.States.OK,
        )

    @cached_property
    def openstack_tenant(self):
        return TenantFactory(
            service_settings=self.openstack_service_settings,
            project=self.project,
            state=openstack_models.Tenant.States.OK,
        )
