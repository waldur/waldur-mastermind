from rest_framework import test

from waldur_mastermind.marketplace.enums import SLURM_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_slurm.tests import fixtures as slurm_fixtures


class OfferingComponentForVolumeTypeTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = slurm_fixtures.SlurmFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type=SLURM_OFFERING,
        )

    def set_option(self, option_name, option_value):
        url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "update_integration"
        )
        new_options = {
            "plugin_options": {option_name: option_value},
        }

        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(url, new_options)

    def test_when_username_generation_policy_is_set_account_name_generation_policy_remains_same(
        self,
    ):
        # Arrange
        initial_policy = "project_slug"
        self.set_option("account_name_generation_policy", initial_policy)

        # Act
        response = self.set_option("username_generation_policy", "waldur_username")

        # Assert
        self.assertEqual(response.status_code, 200)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.plugin_options["account_name_generation_policy"],
            initial_policy,
        )
