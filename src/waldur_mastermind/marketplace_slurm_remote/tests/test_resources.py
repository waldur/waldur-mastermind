from rest_framework import status, test

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace_slurm_remote.tests import (
    fixtures as marketplace_slurm_remote_fixtures,
)
from waldur_slurm import models as slurm_models


class UnlinkTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_slurm_remote_fixtures.MarketplaceSlurmRemoteFixture()
        self.url = factories.ResourceFactory.get_url(
            self.fixture.resource, action="unlink"
        )

    def test_unlink(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)

    def test_unlink_erred_resource(self):
        self.client.force_authenticate(self.fixture.staff)
        self.fixture.resource.state = models.Resource.States.ERRED
        self.fixture.resource.save()
        self.fixture.resource.scope.state = slurm_models.Allocation.States.ERRED
        self.fixture.resource.scope.save()
        response = self.client.post(self.url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)
