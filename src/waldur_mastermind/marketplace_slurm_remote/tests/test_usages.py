from ddt import data, ddt
from django.urls import reverse
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm.tests import factories as slurm_factories


@ddt
class AllocationUserUsageCreationTest(test.APITransactionTestCase):
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
            + 'set_usage'
            + '/'
        )
        self.user = self.fixture.user

        self.new_usage = {
            'cpu_usage': 1,
            'gpu_usage': 2,
            'ram_usage': 3,
            'month': 1,
            'year': 2022,
            'user': structure_factories.UserFactory.get_url(self.user),
            'username': self.user.username,
        }

    @data('staff', 'offering_owner', 'service_manager')
    def test_usage_setting_is_allowed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(self.url, self.new_usage)
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            1,
            slurm_models.AllocationUserUsage.objects.filter(
                allocation=self.allocation,
                username=self.user.username,
                user=self.user,
                month=1,
                year=2022,
            ).count(),
        )
        self.new_usage.update({'user': None, 'username': 'TOTAL_ACCOUNT_USAGE'})
        response = self.client.post(self.url, self.new_usage)
        self.assertEqual(200, response.status_code)
        self.allocation.refresh_from_db()
        self.assertEqual(self.new_usage['cpu_usage'], self.allocation.cpu_usage)
        self.assertEqual(self.new_usage['gpu_usage'], self.allocation.gpu_usage)
        self.assertEqual(self.new_usage['ram_usage'], self.allocation.ram_usage)

    @data('owner', 'admin', 'manager', 'member')
    def test_usage_setting_is_forbidden(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(self.url, self.new_usage)
        self.assertEqual(403, response.status_code)
