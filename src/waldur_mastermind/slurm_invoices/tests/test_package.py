from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.slurm_invoices.tests import factories, fixtures


class SlurmPackageTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.SlurmFixture()
        self.package = self.fixture.package

    def test_authorized_user_can_list_slurm_packages(self):
        self.client.force_login(self.fixture.owner)
        service_settings = structure_factories.ServiceSettingsFactory.get_url(
            self.fixture.service_settings
        )
        response = self.client.get(
            factories.SlurmPackageFactory.get_list_url(),
            {'service_settings': service_settings},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['uuid'], self.package.uuid.hex)

    def test_non_authorized_user_can_not_list_slurm_packages(self):
        self.client.force_login(self.fixture.user)
        response = self.client.get(factories.SlurmPackageFactory.get_list_url())
        self.assertEqual(len(response.data), 0)
