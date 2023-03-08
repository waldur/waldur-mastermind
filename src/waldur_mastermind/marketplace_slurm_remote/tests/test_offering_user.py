from rest_framework import test

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.callbacks import resource_creation_succeeded
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME


class OfferingUserCreationTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        fixture = marketplace_fixtures.MarketplaceFixture()

        self.resource = fixture.resource

        offering = self.resource.offering
        offering.type = PLUGIN_NAME
        offering.secret_options = {'service_provider_can_create_offering_user': True}
        offering.plugin_options = {'username_generation_policy': 'waldur_username'}
        offering.save()

        self.offering_admin = fixture.offering_admin

    def test_offering_user_created_after_role_creation(self):
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()

        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(
                offering=self.resource.offering, user=self.offering_admin
            ).exists()
        )

        self.resource.project.add_user(
            self.offering_admin, structure_models.ProjectRole.ADMINISTRATOR
        )

        self.assertTrue(
            marketplace_models.OfferingUser.objects.filter(
                offering=self.resource.offering, user=self.offering_admin
            ).exists()
        )
        offering_user = marketplace_models.OfferingUser.objects.get(
            offering=self.resource.offering, user=self.offering_admin
        )
        self.assertEqual(offering_user.username, self.offering_admin.username)

    def test_offering_user_created_after_resource_creation(self):
        self.resource.project.add_user(
            self.offering_admin, structure_models.ProjectRole.ADMINISTRATOR
        )
        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(
                offering=self.resource.offering, user=self.offering_admin
            ).exists()
        )

        resource_creation_succeeded(self.resource)

        self.assertTrue(
            marketplace_models.OfferingUser.objects.filter(
                offering=self.resource.offering, user=self.offering_admin
            ).exists()
        )
        offering_user = marketplace_models.OfferingUser.objects.get(
            offering=self.resource.offering, user=self.offering_admin
        )
        self.assertEqual(offering_user.username, self.offering_admin.username)
        self.assertIsNotNone(offering_user.propagation_date)
