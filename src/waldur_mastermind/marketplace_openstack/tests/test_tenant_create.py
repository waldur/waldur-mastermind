from unittest import mock

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace_openstack import views
from waldur_mastermind.marketplace_openstack.tests import fixtures
from waldur_openstack.openstack.tests import factories as openstack_factories


@ddt
class MarketplaceTenantCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceOpenStackFixture()
        self.view = views.MarketplaceTenantViewSet.as_view({'post': 'create'})

    def get_valid_payload(self):
        spl = self.fixture.openstack_spl
        spl_url = openstack_factories.OpenStackServiceProjectLinkFactory.get_url(spl)
        return {
            'service_project_link': spl_url,
            'name': 'test_tenant',
        }

    @data('staff')
    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_he_can_create_tenant(self, user):
        response = common_utils.create_request(
            self.view, getattr(self.fixture, user), self.get_valid_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('user', 'admin', 'owner')
    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_only_staff_manages_services_other_user_can_not_create_tenant(
        self, user
    ):
        response = common_utils.create_request(
            self.view, getattr(self.fixture, user), self.get_valid_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'owner', 'admin')
    def test_user_can_create_tenant(self, user):
        response = common_utils.create_request(
            self.view, getattr(self.fixture, user), self.get_valid_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('user')
    def test_user_cannot_create_tenant(self, user):
        response = common_utils.create_request(
            self.view, getattr(self.fixture, user), self.get_valid_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_skip_connection_extnet_is_false_transfer_false(self):
        transmitted_skip = self._request_with_skip_connection_extnet(False)
        self.assertEqual(transmitted_skip, False)

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=True)
    def test_if_skip_connection_extnet_is_true_transfer_true(self):
        transmitted_skip = self._request_with_skip_connection_extnet(True)
        self.assertEqual(transmitted_skip, True)

    @override_waldur_core_settings(ONLY_STAFF_MANAGES_SERVICES=False)
    def test_transfer_false_if_only_staff_managers_services_is_false(self):
        transmitted_skip = self._request_with_skip_connection_extnet(True)
        self.assertEqual(transmitted_skip, False)

    def _request_with_skip_connection_extnet(self, skip_connection_extnet=False):
        payload = self.get_valid_payload()
        payload['skip_connection_extnet'] = skip_connection_extnet
        patch = mock.patch('waldur_mastermind.marketplace_openstack.views.executors')
        mock_executors = patch.start()
        common_utils.create_request(self.view, self.fixture.staff, payload)
        transmitted_skip = mock_executors.MarketplaceTenantCreateExecutor.execute.call_args[
            1
        ][
            'skip_connection_extnet'
        ]
        mock.patch.stopall()
        return transmitted_skip
