import tomllib
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from ddt import data, ddt
from rest_framework import test

from waldur_core.logging import enums as logging_enums
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.callbacks import resource_creation_succeeded
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    OfferingUserStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_site_agent.tests.fixtures import GlauthUserFixture


def add_user_to_project(user, project, role=None):
    """Helper to add user to project and trigger offering user creation task."""
    if role is None:
        role = ProjectRole.MANAGER
    project.add_user(user, role)
    tasks.create_or_restore_offering_users_for_user(user.uuid.hex, project.uuid.hex)


class OfferingUserCreationTest(test.APITestCase):
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

        add_user_to_project(
            self.offering_admin, self.resource.project, ProjectRole.ADMIN
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

        # Verify required fields exist and have valid values
        # (exact uidnumber/primarygroup values depend on database state)
        self.assertIn("uidnumber", offering_user.backend_metadata)
        self.assertIn("primarygroup", offering_user.backend_metadata)
        self.assertIsInstance(offering_user.backend_metadata["uidnumber"], int)
        self.assertIsInstance(offering_user.backend_metadata["primarygroup"], int)
        self.assertEqual(
            offering_user.backend_metadata["homeDir"],
            f"/tmp/{offering_user.username}",
        )
        self.assertEqual(offering_user.backend_metadata["loginShell"], "/bin/bash")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_message_created_after_resource_creation(
        self, mocked_publish_messages
    ):
        """
        Test that the offering user message is created after the offering user creation.
        """
        event_subscription = logging_factories.EventSubscriptionFactory(
            user=self.offering_admin,
            observable_objects=[
                {"object_type": logging_enums.ObservableObjectType.OFFERING_USER.value}
            ],
        )

        # Create subscription queue (required for messages to be sent)
        logging_factories.EventSubscriptionQueueFactory(
            event_subscription=event_subscription,
            offering_uuid=self.resource.offering.uuid,
            object_type=logging_enums.ObservableObjectType.OFFERING_USER.value,
        )

        self.resource.project.add_user(self.offering_admin, ProjectRole.ADMIN)
        with mock.patch(
            "waldur_mastermind.marketplace.tasks.create_or_restore_offering_users_for_project.delay",
            side_effect=tasks.create_or_restore_offering_users_for_project,
        ):
            with self.captureOnCommitCallbacks(execute=True):
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

        with mock.patch(
            "waldur_mastermind.marketplace.tasks.create_or_restore_offering_users_for_project.delay",
            side_effect=tasks.create_or_restore_offering_users_for_project,
        ):
            with self.captureOnCommitCallbacks(execute=True):
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

        with mock.patch(
            "waldur_mastermind.marketplace.tasks.create_or_restore_offering_users_for_project.delay",
            side_effect=tasks.create_or_restore_offering_users_for_project,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                resource_creation_succeeded(self.resource)
        offering_user = marketplace_models.OfferingUser.objects.get(
            offering=self.resource.offering, user=self.offering_admin
        )
        offering_user2 = marketplace_models.OfferingUser.objects.get(
            offering=self.resource.offering, user=self.offering_owner
        )

        # Verify required fields exist and have valid values
        self.assertIn("uidnumber", offering_user.backend_metadata)
        self.assertIn("primarygroup", offering_user.backend_metadata)
        self.assertIn("homeDir", offering_user.backend_metadata)
        self.assertIn("loginShell", offering_user.backend_metadata)

        # Verify uidnumber and primarygroup are properly set (relative to initial values)
        # The exact values depend on database state, but they should be unique and valid
        uidnumber1 = offering_user.backend_metadata["uidnumber"]
        primarygroup1 = offering_user.backend_metadata["primarygroup"]
        uidnumber2 = offering_user2.backend_metadata["uidnumber"]
        primarygroup2 = offering_user2.backend_metadata["primarygroup"]

        # Verify both users got unique uidnumbers above the initial value
        self.assertGreater(uidnumber1, 1000)  # initial_uidnumber is 1000
        self.assertGreater(uidnumber2, 1000)
        self.assertNotEqual(uidnumber1, uidnumber2)

        # Verify both users got unique primarygroup numbers above the initial value
        self.assertGreater(primarygroup1, 2000)  # initial_primarygroup_number is 2000
        self.assertGreater(primarygroup2, 2000)
        self.assertNotEqual(primarygroup1, primarygroup2)

        # Verify other fields are correctly set
        self.assertEqual(
            offering_user.backend_metadata["homeDir"],
            f"/tmp/{offering_user.username}",
        )
        self.assertEqual(offering_user.backend_metadata["loginShell"], "/bin/bash")

        self.assertEqual(
            offering_user2.backend_metadata["homeDir"],
            f"/tmp/{offering_user2.username}",
        )
        self.assertEqual(offering_user2.backend_metadata["loginShell"], "/bin/bash")


class OfferingUserUpdateTest(test.APITestCase):
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
        self.offering_user.state = OfferingUserStates.CREATING
        self.offering_user.save()

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
class OfferingUserGlauthConfigTest(test.APITestCase):
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

        expected_data = {
            "users": [
                {
                    "name": self.fixture.manager.get_username(),
                    "givenname": self.fixture.manager.first_name,
                    "sn": self.fixture.manager.last_name,
                    "mail": self.fixture.manager.email,
                    "uidnumber": 1001,
                    "primarygroup": 2001,
                    "otherGroups": [6001],
                    "sshkeys": [ssh_key.public_key],
                    "loginShell": "/bin/bash",
                    "homeDir": f"/tmp/{self.fixture.offering_user.username}",
                    "passsha256": "",
                    "disabled": False,
                    "customattributes": {
                        "preferredUsername": [self.fixture.offering_user.username]
                    },
                }
            ],
            "groups": [
                {"name": self.fixture.offering_user.username, "gidnumber": 2001},
                {"name": "6001", "gidnumber": 6001},
                {"name": "6002", "gidnumber": 6002},
            ],
        }
        self.assertEqual(expected_data, tomllib.loads(response.data))

        # Groups must be emitted as [[groups]] array-of-tables, not an inline
        # "groups = [...]" array: the glauth image concatenates this export onto
        # a base config that already declares [[groups]], and a bare groups key
        # collides with it and is silently dropped (so no group reaches LDAP).
        self.assertIn("[[groups]]", response.data)
        self.assertNotIn("groups = [", response.data)

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

    def test_glauth_config_escapes_backslashes_in_ssh_keys(self):
        """SSH keys with backslashes in comments must be escaped for valid TOML."""
        structure_factories.SshPublicKeyFactory(
            user=self.fixture.manager,
            public_key=r"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINweobRRnzaUEIM5nbLFGm/MuFcioMwFtKkycv2m781l ul\sd41041@LAP-113622",
        )
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.fixture.url)
        self.assertEqual(200, response.status_code)
        self.assertIn(r"ul\\sd41041@LAP-113622", response.data)
        self.assertNotIn(r"ul\sd41041@LAP-113622", response.data)

    def test_glauth_config_handles_special_characters_robustly(self):
        """User attributes containing backslashes and double quotes must be successfully serialized and parsed."""
        self.fixture.offering.plugin_options["emit_display_name"] = True
        self.fixture.offering.save(update_fields=["plugin_options"])

        manager = self.fixture.manager
        manager.first_name = 'John "Johnny"'
        manager.last_name = r"Doe\Smith"
        manager.save()

        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.fixture.url)
        self.assertEqual(200, response.status_code)

        # Parse TOML to ensure validity
        parsed_config = tomllib.loads(response.data)
        user_record = parsed_config["users"][0]
        self.assertEqual(user_record["givenname"], 'John "Johnny"')
        self.assertEqual(user_record["sn"], r"Doe\Smith")
        self.assertEqual(
            user_record["customattributes"]["displayName"], ['John "Johnny" Doe\\Smith']
        )


@ddt
class OfferingUserGlauthConfigQueryCountTest(test.APITestCase):
    """Test that glauth_users_config endpoint has constant query count regardless of user count."""

    def setUp(self) -> None:
        self.fixture = GlauthUserFixture()

    @data(5, 10)
    def test_query_count_does_not_scale_with_user_count(self, num_users):
        """
        Verify that the number of queries does not grow linearly with the number of users.
        This test creates multiple offering users and verifies query count stays bounded.

        Before optimization: ~5 queries per user (user, ssh_keys, projects, groups, resources)
        would result in ~55 queries for 10 users.
        After optimization: should be constant (~5-10 queries) regardless of user count.
        """
        from django.db import connection, reset_queries
        from django.test.utils import override_settings

        offering = self.fixture.offering

        # Create additional users with offering users
        for i in range(num_users):
            user = structure_factories.UserFactory(username=f"testuser{i}")
            self.fixture.project.add_user(user, ProjectRole.MEMBER)
            # Create offering user with required metadata
            offering_user = marketplace_models.OfferingUser.objects.create(
                offering=offering,
                user=user,
                username=f"testuser{i}",
            )
            marketplace_utils.setup_linux_related_data(offering_user, offering)
            offering_user.save()
            # Add SSH key for each user
            structure_factories.SshPublicKeyFactory(user=user)

        # Prepare queryset as done in views (with select_related and prefetch_related)
        offering_users = (
            marketplace_models.OfferingUser.objects.filter(offering=offering)
            .exclude(username="")
            .select_related("user")
            .prefetch_related("user__sshpublickey_set")
        )

        # Enable query logging and count queries
        with override_settings(DEBUG=True):
            reset_queries()

            marketplace_utils.generate_glauth_records_for_offering_users(
                offering, offering_users
            )

            query_count = len(connection.queries)

        # After optimization: query count should be constant regardless of user count
        # Expected queries:
        # 1. Evaluate offering_users queryset (with select_related user)
        # 2. Prefetch SSH keys
        # 3. Get ContentType for Project
        # 4. Batch query UserRole for user->project mapping
        # 5. Batch query OfferingUserGroup for project->gid mapping
        # 6. Batch query Resource for users with active resources
        # Total: ~6-10 queries max
        max_allowed_queries = 15
        self.assertLess(
            query_count,
            max_allowed_queries,
            f"Query count ({query_count}) for {num_users} users exceeds limit ({max_allowed_queries}). "
            f"This suggests N+1 query problem. Expected constant query count regardless of user count.",
        )


class UidNumberPerOfferingScopeTest(test.APITestCase):
    """Test that uidnumber generation is scoped per offering, not global."""

    def test_uidnumbers_are_sequential_per_offering(self):
        """
        When two offerings exist, creating users in offering A should not
        create gaps in uidnumber sequence of offering B.
        """
        fixture = marketplace_fixtures.MarketplaceFixture()

        offering_a = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            customer=fixture.offering_customer,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "anonymized",
                "username_anonymized_prefix": "user_a_",
                "initial_uidnumber": 10000,
                "initial_primarygroup_number": 1000,
                "homedir_prefix": "/home/",
            },
        )
        offering_b = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            customer=fixture.offering_customer,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "anonymized",
                "username_anonymized_prefix": "user_b_",
                "initial_uidnumber": 10000,
                "initial_primarygroup_number": 1000,
                "homedir_prefix": "/home/",
            },
        )

        # Create 3 users in offering A
        for i in range(3):
            user = structure_factories.UserFactory()
            ou = marketplace_models.OfferingUser.objects.create(
                offering=offering_a,
                user=user,
                username=f"user_a_{str(i).zfill(5)}",
            )
            marketplace_utils.setup_linux_related_data(ou, offering_a)
            ou.save()

        # Create 2 users in offering B
        offering_b_users = []
        for i in range(2):
            user = structure_factories.UserFactory()
            ou = marketplace_models.OfferingUser.objects.create(
                offering=offering_b,
                user=user,
                username=f"user_b_{str(i).zfill(5)}",
            )
            marketplace_utils.setup_linux_related_data(ou, offering_b)
            ou.save()
            offering_b_users.append(ou)

        # Offering B's first user should get uidnumber 10001, not 10004
        self.assertEqual(offering_b_users[0].backend_metadata["uidnumber"], 10001)
        self.assertEqual(offering_b_users[0].backend_metadata["primarygroup"], 1001)

        # Offering B's second user should get uidnumber 10002, not 10005
        self.assertEqual(offering_b_users[1].backend_metadata["uidnumber"], 10002)
        self.assertEqual(offering_b_users[1].backend_metadata["primarygroup"], 1002)

    def test_uidnumbers_are_sequential_within_single_offering(self):
        """
        Verify that uidnumbers increment sequentially within one offering.
        """
        fixture = marketplace_fixtures.MarketplaceFixture()

        offering = marketplace_factories.OfferingFactory(
            type=SITE_AGENT_OFFERING,
            customer=fixture.offering_customer,
            plugin_options={
                "service_provider_can_create_offering_user": True,
                "username_generation_policy": "waldur_username",
                "initial_uidnumber": 5000,
                "initial_primarygroup_number": 6000,
                "homedir_prefix": "/home/",
            },
        )

        users = []
        for i in range(4):
            user = structure_factories.UserFactory()
            ou = marketplace_models.OfferingUser.objects.create(
                offering=offering,
                user=user,
                username=user.username,
            )
            marketplace_utils.setup_linux_related_data(ou, offering)
            ou.save()
            users.append(ou)

        for i, ou in enumerate(users):
            expected_uid = 5001 + i
            expected_group = 6001 + i
            self.assertEqual(
                ou.backend_metadata["uidnumber"],
                expected_uid,
                f"User {i} expected uidnumber {expected_uid}, got {ou.backend_metadata['uidnumber']}",
            )
            self.assertEqual(
                ou.backend_metadata["primarygroup"],
                expected_group,
                f"User {i} expected primarygroup {expected_group}, got {ou.backend_metadata['primarygroup']}",
            )


@override_constance_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class UserOfferingsMappingTest(test.APITestCase):
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
