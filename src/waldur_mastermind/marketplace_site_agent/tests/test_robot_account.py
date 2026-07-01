import tomllib

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING,
    ResourceStates,
    RobotAccountStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class RobotAccountGlauthConfigTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.plugin_options = {
            "username_generation_policy": "waldur_username",
            "initial_uidnumber": 1000,
            "initial_primarygroup_number": 2000,
            "service_provider_can_create_offering_user": True,
        }
        self.offering.save()

        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.robot_account = marketplace_factories.RobotAccountFactory(
            state=RobotAccountStates.OK, resource=self.resource
        )
        marketplace_utils.setup_linux_related_data(self.robot_account, self.offering)
        self.robot_account.save()

        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "glauth_users_config"
        )
        self.maxDiff = None

    def test_glauth_config_file_fetching(self):
        ssh_key = structure_factories.SshPublicKeyFactory()
        self.robot_account.keys = [ssh_key.public_key]
        self.robot_account.save()
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)

        expected_data = {
            "users": [
                {
                    "name": self.robot_account.username,
                    "uidnumber": 1001,
                    "primarygroup": 2001,
                    "sshkeys": [ssh_key.public_key],
                    "loginShell": "/bin/bash",
                    "homeDir": f"/home/{self.robot_account.username}",
                    "passsha256": "",
                    "customattributes": {
                        "preferredUsername": [self.robot_account.username]
                    },
                }
            ],
            "groups": [{"name": self.robot_account.username, "gidnumber": 2001}],
        }
        self.assertEqual(expected_data, tomllib.loads(response.data))

    def test_glauth_exposes_correct_states(self):
        """Test that only OK and REQUESTED_DELETION states are exposed in glauth due to filtering by state"""
        # Create additional robot accounts in different states
        requested_deletion_account = marketplace_factories.RobotAccountFactory(
            resource=self.resource,
            username="test2",
            state=RobotAccountStates.REQUESTED_DELETION,
            type="test2",
        )
        # This should not be exposed in glauth
        creating_account = marketplace_factories.RobotAccountFactory(
            resource=self.resource,
            username="test3",
            state=RobotAccountStates.CREATING,
            type="test3",
        )
        # Set up Linux-related data for new accounts
        for account in [requested_deletion_account, creating_account]:
            marketplace_utils.setup_linux_related_data(account, self.offering)
            account.save()

        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Expected status code 200, but got {response.status_code}",
        )
        content = response.content.decode("utf-8")
        # Check that OK and REQUESTED_DELETION accounts are included
        self.assertIn(
            self.robot_account.username,
            content,
            f"Expected {self.robot_account.username} to be included",
        )
        self.assertIn(
            requested_deletion_account.username,
            content,
            f"Expected {requested_deletion_account.username} to be included",
        )

        # Check that other states are not included
        self.assertNotIn(
            creating_account.username,
            content,
            f"Expected {creating_account.username} to be excluded",
        )
