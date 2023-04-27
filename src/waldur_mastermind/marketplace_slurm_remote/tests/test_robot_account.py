import textwrap

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME


class RobotAccountGlauthConfigTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = PLUGIN_NAME
        self.offering.plugin_options = {
            'initial_uidnumber': 1000,
            'initial_primarygroup_number': 2000,
        }
        self.offering.save()

        self.resource = self.fixture.resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()

        self.robot_account = marketplace_factories.RobotAccountFactory(
            resource=self.resource
        )
        self.url = marketplace_factories.OfferingFactory.get_url(
            self.offering, 'glauth_users_config'
        )
        self.maxDiff = None

    def test_glauth_config_file_fetching(self):
        ssh_key = structure_factories.SshPublicKeyFactory()
        self.robot_account.keys = [ssh_key.public_key]
        self.robot_account.save()
        self.client.force_login(self.fixture.offering_owner)
        response = self.client.get(self.url)

        expected_config_file = textwrap.dedent(
            f"""
        [[users]]
          name = "{self.robot_account.username}"
          uidnumber = 1001
          primarygroup = 2001
          sshkeys = ["{ssh_key.public_key}"]
          passsha256 = ""
            [[users.customattributes]]
            preferredUsername = ["{self.robot_account.username}"]

        [[groups]]
          name = "{self.robot_account.username}"
          gidnumber = 2001
        """
        )

        self.assertEqual(expected_config_file, response.data)
