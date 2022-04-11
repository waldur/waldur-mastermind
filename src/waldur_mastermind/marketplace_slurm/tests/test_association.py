from ddt import data, ddt
from django.urls import reverse
from rest_framework import test

from waldur_freeipa.tests import factories
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm.tests import factories as slurm_factories


@ddt
class AssociationCreateTest(test.APITransactionTestCase):
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

        self.user = self.fixture.user
        self.url = (
            'http://testserver'
            + reverse(
                'marketplace-slurm-detail', kwargs={'uuid': self.resource.uuid.hex}
            )
            + 'create_association'
            + '/'
        )
        self.profile = factories.ProfileFactory(
            user=self.user, username=self.user.username
        )
        self.username = self.profile.username

    @data('staff', 'offering_owner', 'service_manager')
    def test_association_creation_is_allowed(self, user):
        self.assertFalse(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                'username': self.username,
            },
        )
        self.assertEqual(201, response.status_code)
        self.assertTrue(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )

    @data('owner', 'admin', 'manager', 'member')
    def test_association_creation_is_forbidden(self, user):
        self.assertFalse(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                'username': self.username,
            },
        )
        self.assertEqual(403, response.status_code)
        self.assertFalse(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )


@ddt
class AssociationDeleteTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.user = self.fixture.user
        self.profile = factories.ProfileFactory(
            user=self.user, username=self.user.username
        )
        self.username = self.profile.username
        self.association = slurm_factories.AssociationFactory(
            username=self.user.username
        )
        self.allocation = self.association.allocation
        self.resource.scope = self.allocation
        self.resource.save()
        offering = self.resource.offering
        offering.type = PLUGIN_NAME
        offering.save()

        self.url = (
            'http://testserver'
            + reverse(
                'marketplace-slurm-detail', kwargs={'uuid': self.resource.uuid.hex}
            )
            + 'delete_association'
            + '/'
        )

    @data('staff', 'offering_owner', 'service_manager')
    def test_association_deletion_is_allowed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                'username': self.username,
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertFalse(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )

    @data('owner', 'admin', 'manager', 'member')
    def test_association_creation_is_forbidden(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                'username': self.username,
            },
        )
        self.assertEqual(403, response.status_code)
        self.assertTrue(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )
