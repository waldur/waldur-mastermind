from ddt import data, ddt
from django.urls import reverse
from rest_framework import test

from waldur_freeipa.tests import factories as freeipa_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME
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
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()
        offering = self.resource.offering
        offering.type = PLUGIN_NAME
        offering.secret_options = {"service_provider_can_create_offering_user": True}
        offering.plugin_options = {"username_generation_policy": "waldur_username"}
        offering.save()

        self.user = self.fixture.admin
        self.url = (
            "http://testserver"
            + reverse(
                "marketplace-slurm-remote-detail",
                kwargs={"uuid": self.resource.uuid.hex},
            )
            + "create_association"
            + "/"
        )
        self.profile = freeipa_factories.ProfileFactory(
            user=self.user, username=self.user.username
        )
        self.username = self.profile.username

    @data("staff", "offering_owner", "service_manager")
    def test_association_creation_is_allowed(self, user):
        offering_user = marketplace_models.OfferingUser.objects.get(
            offering=self.resource.offering, user=self.user, propagation_date=None
        )
        self.assertFalse(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                "username": self.username,
            },
        )
        self.assertEqual(201, response.status_code)
        self.assertTrue(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )
        offering_user.refresh_from_db()
        self.assertIsNotNone(
            offering_user.propagation_date, offering_user.propagation_date
        )

    @data("owner", "admin", "manager", "member")
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
                "username": self.username,
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
        self.profile = freeipa_factories.ProfileFactory(
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
            "http://testserver"
            + reverse(
                "marketplace-slurm-remote-detail",
                kwargs={"uuid": self.resource.uuid.hex},
            )
            + "delete_association"
            + "/"
        )

    @data("staff", "offering_owner", "service_manager")
    def test_association_deletion_is_allowed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                "username": self.username,
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertFalse(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )

    @data("owner", "admin", "manager", "member")
    def test_association_creation_is_forbidden(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(
            self.url,
            {
                "username": self.username,
            },
        )
        self.assertEqual(403, response.status_code)
        self.assertTrue(
            slurm_models.Association.objects.filter(
                username=self.username, allocation=self.allocation
            ).exists()
        )
