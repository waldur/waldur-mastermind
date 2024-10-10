from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests.factories import ProjectFactory
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace_openstack import views
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests.fixtures import OpenStackFixture


@ddt
class MarketplaceTenantCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()
        self.view = views.MarketplaceTenantViewSet.as_view({"post": "create"})

    def get_valid_payload(self):
        return {
            "service_settings": openstack_factories.SettingsFactory.get_url(
                self.fixture.settings
            ),
            "project": ProjectFactory.get_url(self.fixture.project),
            "name": "test_tenant",
        }

    @data("staff", "owner", "admin")
    def test_user_can_create_tenant(self, user):
        response = common_utils.create_request(
            self.view, getattr(self.fixture, user), self.get_valid_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("user")
    def test_user_cannot_create_tenant(self, user):
        response = common_utils.create_request(
            self.view, getattr(self.fixture, user), self.get_valid_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(True, False)
    def test_skip_connection_extnet(self, value):
        payload = self.get_valid_payload()
        payload["skip_connection_extnet"] = value
        with mock.patch(
            "waldur_mastermind.marketplace_openstack.views.TenantCreateExecutor"
        ) as patcher:
            common_utils.create_request(self.view, self.fixture.staff, payload)
            self.assertEqual(
                value, patcher.execute.call_args[1]["skip_connection_extnet"]
            )
