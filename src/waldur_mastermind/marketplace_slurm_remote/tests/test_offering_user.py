import textwrap

from rest_framework import test

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.callbacks import resource_creation_succeeded
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME


class OfferingUserCreationTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        fixture = marketplace_fixtures.MarketplaceFixture()

        self.resource = fixture.resource

        offering = self.resource.offering
        offering.type = PLUGIN_NAME
        offering.secret_options = {'service_provider_can_create_offering_user': True}
        offering.plugin_options = {
            'username_generation_policy': 'waldur_username',
            'initial_uidnumber': 1000,
            'initial_primarygroup_number': 2000,
        }
        offering.save()

        self.offering_admin = fixture.offering_admin
        self.offering_owner = fixture.offering_owner

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
        self.assertEqual(
            offering_user.backend_metadata,
            {
                'uidnumber': 1001,
                'primarygroup': 2001,
                'homeDir': f'/home/{offering_user.username}',
                'loginShell': '/bin/sh',
            },
        )

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

    def test_offering_user_unix_data(self):
        self.resource.project.add_user(
            self.offering_admin, structure_models.ProjectRole.ADMINISTRATOR
        )
        self.resource.project.add_user(
            self.offering_owner, structure_models.ProjectRole.MANAGER
        )

        resource_creation_succeeded(self.resource)
        offering_user = marketplace_models.OfferingUser.objects.get(
            offering=self.resource.offering, user=self.offering_admin
        )
        offering_user2 = marketplace_models.OfferingUser.objects.get(
            offering=self.resource.offering, user=self.offering_owner
        )
        self.assertEqual(
            offering_user.backend_metadata,
            {
                'uidnumber': 1001,
                'primarygroup': 2001,
                'homeDir': f'/home/{offering_user.username}',
                'loginShell': '/bin/sh',
            },
        )
        self.assertEqual(
            offering_user2.backend_metadata,
            {
                'uidnumber': 1002,
                'primarygroup': 2002,
                'homeDir': f'/home/{offering_user2.username}',
                'loginShell': '/bin/sh',
            },
        )


class OfferingUserUpdateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        fixture = marketplace_fixtures.MarketplaceFixture()

        self.resource = fixture.resource
        self.resource.set_state_ok()
        self.resource.save()

        self.offering = self.resource.offering
        self.offering.type = PLUGIN_NAME
        self.offering.secret_options = {
            'service_provider_can_create_offering_user': True
        }
        self.offering.plugin_options = {
            'username_generation_policy': 'waldur_username',
        }
        self.offering.save()

        self.admin = fixture.admin
        self.offering_user = marketplace_models.OfferingUser.objects.get(
            user=self.admin,
            offering=self.offering,
        )

    def test_username_updated_when_generation_policy_changed(self):
        self.assertEqual(self.admin.username, self.offering_user.username)

        self.offering.plugin_options['username_generation_policy'] = 'anonymized'
        self.offering.save(update_fields=['plugin_options'])

        self.offering_user.refresh_from_db()

        self.assertEqual(
            self.offering_user.username,
            'walduruser_00000',
        )


class TestOfferingUser(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()

        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.plugin_options = {
            'username_generation_policy': 'waldur_username',
            'initial_uidnumber': 1000,
            'initial_primarygroup_number': 2000,
        }
        self.offering.secret_options = {
            'service_provider_can_create_offering_user': True
        }
        self.offering.save()

        self.resource = self.fixture.resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()

        self.manager = self.fixture.manager
        self.offering_user = marketplace_models.OfferingUser.objects.get(
            offering=self.offering, user=self.manager
        )
        self.offering_user.set_propagation_date()
        self.offering_user.save()

        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, 'glauth_users_config'
        )
        self.maxDiff = None

    def test_galuth_config_file_fetching_not_allowed(self):
        self.client.force_login(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(404, response.status_code)

    def test_galuth_config_file_fetching(self):
        ssh_key = structure_factories.SshPublicKeyFactory(user=self.manager)
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)

        expected_config_file = textwrap.dedent(
            f"""
        [[users]]
          name = "{self.manager.get_username()}"
          givenname="{self.manager.first_name}"
          sn="{self.manager.last_name}"
          mail = "{self.manager.email}"
          uidnumber = 1001
          primarygroup = 2001
          sshkeys = ["{ssh_key.public_key}"]
          loginShell = "/bin/sh"
          homeDir = "/home/{self.offering_user.username}"
          passsha256 = ""
            [[users.customattributes]]
            preferredUsername = ["{self.offering_user.username}"]

        [[groups]]
          name = "{self.offering_user.username}"
          gidnumber = 2001
        """
        )
        self.assertEqual(expected_config_file, response.data)
