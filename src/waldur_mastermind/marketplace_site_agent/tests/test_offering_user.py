import textwrap
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from ddt import data, ddt
from rest_framework import test

from waldur_core.logging import utils as logging_utils
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.callbacks import resource_creation_succeeded
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent.tests.fixtures import GlauthUserFixture


class OfferingUserCreationTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        fixture = marketplace_fixtures.MarketplaceFixture()

        self.resource = fixture.resource

        offering = self.resource.offering
        offering.type = SITE_AGENT_OFFERING
        offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
            "initial_uidnumber": 1000,
            "initial_primarygroup_number": 2000,
            "homedir_prefix": "/tmp/",
        }
        offering.save()

        self.offering_admin = fixture.offering_admin
        self.offering_owner = fixture.offering_owner

    def test_offering_user_created_after_role_creation(self):
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(
                offering=self.resource.offering, user=self.offering_admin
            ).exists()
        )

        self.resource.project.add_user(self.offering_admin, ProjectRole.ADMIN)

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
                "uidnumber": 1001,
                "primarygroup": 2001,
                "homeDir": f"/tmp/{offering_user.username}",
                "loginShell": "/bin/bash",
            },
        )

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_message_created_after_resource_creation(
        self, mocked_publish_messages
    ):
        """
        Test that the offering user message is created after the offering user creation.
        """
        logging_factories.EventSubscriptionFactory(
            user=self.offering_admin,
            observable_objects=[
                {"object_type": logging_utils.ObservableObjectType.OFFERING_USER.value}
            ],
        )

        self.resource.project.add_user(self.offering_admin, ProjectRole.ADMIN)
        resource_creation_succeeded(self.resource)

        # Verify that publish_messages.delay was called
        mocked_publish_messages.assert_called()

        message = mocked_publish_messages.call_args[0][0][0]
        offering_user = marketplace_models.OfferingUser.objects.get(
            offering=self.resource.offering, user=self.offering_admin
        )
        # Check that the message contains the offering admin username
        self.assertEqual(message["vhost"], self.offering_admin.uuid.hex)
        self.assertIn(self.offering_admin.username, message["payload"])
        self.assertIn(self.offering_admin.uuid.hex, message["payload"])
        self.assertIn(offering_user.uuid.hex, message["payload"])
        self.assertIn("state", str(message["payload"]))
        self.assertIn("username_set", message["payload"])

    def test_offering_user_created_after_resource_creation(self):
        self.resource.project.add_user(self.offering_admin, ProjectRole.ADMIN)
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

    def test_offering_user_unix_data(self):
        self.resource.project.add_user(self.offering_admin, ProjectRole.ADMIN)
        self.resource.project.add_user(self.offering_owner, ProjectRole.MANAGER)

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
                "uidnumber": 1001,
                "primarygroup": 2001,
                "homeDir": f"/tmp/{offering_user.username}",
                "loginShell": "/bin/bash",
            },
        )
        self.assertEqual(
            offering_user2.backend_metadata,
            {
                "uidnumber": 1002,
                "primarygroup": 2002,
                "homeDir": f"/tmp/{offering_user2.username}",
                "loginShell": "/bin/bash",
            },
        )


class OfferingUserUpdateTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        fixture = marketplace_fixtures.MarketplaceFixture()

        self.resource = fixture.resource
        self.resource.set_state_ok()
        self.resource.save()

        self.offering = self.resource.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
            "homedir_prefix": "/tmp/",
        }
        self.offering.save()

        self.admin = fixture.admin
        self.offering_user = marketplace_models.OfferingUser.objects.get(
            user=self.admin,
            offering=self.offering,
        )

    def test_username_updated_when_generation_policy_changed(self):
        self.assertEqual(self.admin.username, self.offering_user.username)
        self.assertEqual(
            f"/tmp/{self.admin.username}",
            self.offering_user.backend_metadata["homeDir"],
        )

        self.offering.plugin_options["username_generation_policy"] = "anonymized"
        self.offering.save(update_fields=["plugin_options"])

        self.offering_user.refresh_from_db()

        self.assertEqual(
            self.offering_user.username,
            "walduruser_00000",
        )
        self.assertEqual(
            "/tmp/walduruser_00000", self.offering_user.backend_metadata["homeDir"]
        )

    def test_username_updated_when_generation_policy_changed_to_service_provider(self):
        self.assertEqual(self.admin.username, self.offering_user.username)

        self.offering.plugin_options["username_generation_policy"] = "service_provider"
        self.offering.save(update_fields=["plugin_options"])

        self.offering_user.refresh_from_db()

        self.assertEqual(
            self.offering_user.username,
            "",
        )


@ddt
class OfferingUserGlauthConfigTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = GlauthUserFixture()
        self.maxDiff = None

    def test_glauth_config_user_disabled_when_no_resources(self):
        """
        Test that the user is disabled when there are no resources.
        """
        offering_user = self.fixture.offering_user_without_resources
        url = marketplace_factories.OfferingFactory.get_url(
            offering_user.offering, "glauth_users_config"
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(url)
        self.assertEqual(
            200,
            response.status_code,
            f"Expected status code 200, but got {response.status_code}",
        )
        self.assertIn(
            "disabled = true",
            response.data,
            f"Expected disabled = true, but got {response.data}",
        )

    def test_glauth_config_user_disabled_when_only_terminated_resources(self):
        """
        Test that the user is disabled when there are only terminated resources.
        """
        offering_user = self.fixture.offering_user_with_terminated_resource
        url = marketplace_factories.OfferingFactory.get_url(
            offering_user.offering, "glauth_users_config"
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(url)
        self.assertEqual(
            200,
            response.status_code,
            f"Expected status code 200, but got {response.status_code}",
        )
        self.assertIn(
            "disabled = true",
            response.data,
            f"Expected disabled = true, but got {response.data}",
        )

    def test_glauth_config_file_fetching_not_allowed(self):
        self.client.force_login(self.fixture.owner)
        response = self.client.get(self.fixture.url)
        self.assertEqual(404, response.status_code)

    @data("offering_owner", "service_manager", "offering_manager")
    def test_glauth_config_file_fetching(self, user_role):
        ssh_key = structure_factories.SshPublicKeyFactory(user=self.fixture.manager)
        user = getattr(self.fixture, user_role)
        self.client.force_login(user)
        self.assertEqual(
            0,
            marketplace_models.IntegrationStatus.objects.filter(
                offering=self.fixture.offering,
                agent_type=marketplace_models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
                status=marketplace_models.IntegrationStatus.States.ACTIVE,
            ).count(),
        )
        response = self.client.get(self.fixture.url)
        self.assertEqual(200, response.status_code)

        expected_config_file = textwrap.dedent(
            f"""
        [[users]]
          name = "{self.fixture.manager.get_username()}"
          givenname="{self.fixture.manager.first_name}"
          sn="{self.fixture.manager.last_name}"
          mail = "{self.fixture.manager.email}"
          uidnumber = 1001
          primarygroup = 2001
          otherGroups = [6001]
          sshkeys = ["{ssh_key.public_key}"]
          loginShell = "/bin/bash"
          homeDir = "/tmp/{self.fixture.offering_user.username}"
          passsha256 = ""
          disabled = false
            [[users.customattributes]]
            preferredUsername = ["{self.fixture.offering_user.username}"]

        [[groups]]
          name = "{self.fixture.offering_user.username}"
          gidnumber = 2001


        [[groups]]
          name = "6001"
          gidnumber = 6001


        [[groups]]
          name = "6002"
          gidnumber = 6002
        """
        )
        self.assertEqual(expected_config_file, response.data)

        self.assertEqual(
            1,
            marketplace_models.IntegrationStatus.objects.filter(
                offering=self.fixture.offering,
                agent_type=marketplace_models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
                status=marketplace_models.IntegrationStatus.States.ACTIVE,
            ).count(),
        )
        integration_status = marketplace_models.IntegrationStatus.objects.get(
            offering=self.fixture.offering,
            agent_type=marketplace_models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
            status=marketplace_models.IntegrationStatus.States.ACTIVE,
        )
        self.assertIsNotNone(integration_status.last_request_timestamp)


@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class UserOfferingsMappingTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.offering = self.resource.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
        }
        self.offering.save()

        marketplace_models.OfferingTermsOfService.objects.create(
            offering=self.offering, terms_of_service="Test ToS", is_active=True
        )

        self.user = self.fixture.offering_admin
        self.resource.project.add_user(self.user, ProjectRole.ADMIN)

        marketplace_models.OfferingUser.objects.filter(
            user=self.user, offering=self.offering
        ).delete()

    def test_user_offerings_mapping_creates_offering_user_with_consent(self):
        """Test that user_offerings_mapping creates offering users for users with active consent."""

        marketplace_models.UserOfferingConsent.objects.create(
            user=self.user, offering=self.offering, version="1.0"
        )

        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(
                user=self.user, offering=self.offering
            ).exists()
        )

        marketplace_utils.user_offerings_mapping([self.offering])

        self.assertTrue(
            marketplace_models.OfferingUser.objects.filter(
                user=self.user, offering=self.offering
            ).exists()
        )

    def test_user_offerings_mapping_skips_user_without_consent(self):
        """Test that user_offerings_mapping skips users without consent."""

        marketplace_utils.user_offerings_mapping([self.offering])

        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(
                user=self.user, offering=self.offering
            ).exists()
        )

    def test_user_offerings_mapping_skips_user_with_revoked_consent(self):
        """Test that user_offerings_mapping skips users with revoked consent."""

        consent = marketplace_models.UserOfferingConsent.objects.create(
            user=self.user, offering=self.offering, version="1.0"
        )
        consent.revoke()

        marketplace_utils.user_offerings_mapping([self.offering])

        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(
                user=self.user, offering=self.offering
            ).exists()
        )
