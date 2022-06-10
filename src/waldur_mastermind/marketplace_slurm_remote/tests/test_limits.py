from ddt import data, ddt
from django.urls import reverse
from rest_framework import test

from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME
from waldur_slurm.tests import factories as slurm_factories


@ddt
class AllocationSetLimits(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.allocation = slurm_factories.AllocationFactory(
            project=self.fixture.project
        )
        self.resource.scope = self.allocation
        self.resource.save()
        offering = self.resource.offering
        offering.type = PLUGIN_NAME
        offering.save()

        self.url = (
            'http://testserver'
            + reverse(
                'marketplace-slurm-remote-detail',
                kwargs={'uuid': self.resource.uuid.hex},
            )
            + 'set_limits'
            + '/'
        )

        self.new_limits = {
            'cpu_limit': 1,
            'gpu_limit': 2,
            'ram_limit': 3,
        }

    @data('staff', 'offering_owner', 'service_manager')
    def test_limits_setting_is_allowed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(self.url, self.new_limits)
        self.assertEqual(200, response.status_code)
        self.allocation.refresh_from_db()
        self.assertEqual(
            self.new_limits,
            {
                'cpu_limit': self.allocation.cpu_limit,
                'gpu_limit': self.allocation.gpu_limit,
                'ram_limit': self.allocation.ram_limit,
            },
        )

    @data('owner', 'admin', 'manager', 'member')
    def test_limits_setting_is_forbidden(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(self.url, self.new_limits)
        self.assertEqual(403, response.status_code)
