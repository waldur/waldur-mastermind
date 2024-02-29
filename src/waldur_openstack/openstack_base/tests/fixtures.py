from unittest import mock

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
            options={"external_network_id": "test_network_id"},
            state=structure_models.ServiceSettings.States.OK,
        )

    @cached_property
    def openstack_tenant(self):
        return TenantFactory(
            service_settings=self.openstack_service_settings,
            project=self.project,
            state=openstack_models.Tenant.States.OK,
        )


def mock_session():
    session_mock = mock.patch("keystoneauth1.session.Session").start()()
    session_mock.auth.auth_url = "auth_url"
    session_mock.auth.project_id = "project_id"
    session_mock.auth.project_domain_name = None
    session_mock.auth.project_name = None
    session_mock.auth.auth_ref.auth_token = "token"
    session_mock.auth.get_auth_state.return_value = ""
