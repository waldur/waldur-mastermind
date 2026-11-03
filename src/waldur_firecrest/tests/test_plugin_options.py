from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace.enums import SITE_AGENT_OFFERING
from waldur_mastermind.marketplace.tests import factories


class FirecrestPluginOptionsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=SITE_AGENT_OFFERING
        )
        self.url = factories.OfferingFactory.get_url(
            self.offering, "update_integration"
        )
        self.client.force_authenticate(self.fixture.owner)

    def test_options_persist(self):
        options = {
            "firecrest_api_url": "http://firecrest:5000",
            "firecrest_system_name": "slurm-a",
            "slurm_monitor_api_url": "http://slurm-monitor:12000/api/v2",
            "slurm_monitor_cluster_name": "hpc-demo",
        }
        response = self.client.post(self.url, {"plugin_options": options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        for key, value in options.items():
            self.assertEqual(self.offering.plugin_options[key], value)

    def test_non_http_url_is_rejected(self):
        response = self.client.post(
            self.url, {"plugin_options": {"firecrest_api_url": "ssh://login01"}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
