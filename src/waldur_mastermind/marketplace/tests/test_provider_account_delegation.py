"""What a provider-backed offering account exposes, and what it refuses."""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.enums import AccountScopes, OfferingUserStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class ProviderBackedOfferingUserTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()
        self.offering_a = self.fixture.offering
        self.offering_b = factories.OfferingFactory(customer=self.provider.customer)
        self.user = structure_factories.UserFactory()

        self.account = models.ServiceProviderAccount.objects.create(
            service_provider=self.provider,
            user=self.user,
            username="jsmith",
            state=OfferingUserStates.OK,
            backend_metadata={
                "uidnumber": 9001,
                "primarygroup": 9001,
                "loginShell": "/bin/bash",
                "homeDir": "/home/jsmith",
            },
        )
        self.offering_users = []
        for offering in (self.offering_a, self.offering_b):
            offering_user = models.OfferingUser.objects.create(
                offering=offering, user=self.user, service_provider_account=self.account
            )
            offering_user.pull_from_provider_account()
            offering_user.save()
            self.offering_users.append(offering_user)

    def _list(self, offering):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.get(
            factories.OfferingUserFactory.get_list_url(),
            {"offering_uuid": offering.uuid.hex},
        )

    def test_both_offerings_report_the_same_account_over_the_api(self):
        """The headline claim: one person, one identity, across the provider."""
        seen = []
        for offering in (self.offering_a, self.offering_b):
            response = self._list(offering)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), 1)
            row = response.data[0]
            seen.append(
                (
                    row["username"],
                    row["uidnumber"],
                    row["primarygroup"],
                    row["home_directory"],
                    row["login_shell"],
                )
            )
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(seen[0], ("jsmith", 9001, 9001, "/home/jsmith", "/bin/bash"))

    def test_only_one_provider_account_backs_them(self):
        self.assertEqual(
            models.ServiceProviderAccount.objects.filter(
                service_provider=self.provider, user=self.user
            ).count(),
            1,
        )
        for offering_user in self.offering_users:
            offering_user.refresh_from_db()
            self.assertEqual(offering_user.service_provider_account, self.account)

    def test_writing_the_username_on_a_backed_account_is_refused(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.OfferingUserFactory.get_url(self.offering_users[0]),
            {"username": "someone-else"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The message has to say where the real edit goes, or it is just a wall.
        self.assertIn(self.account.uuid.hex, str(response.data))
        self.offering_users[0].refresh_from_db()
        self.assertEqual(self.offering_users[0].username, "jsmith")

    def test_a_change_on_the_provider_account_reaches_every_offering(self):
        self.account.username = "j.smith"
        self.account.save()
        utils.propagate_provider_account(self.account)

        for offering_user in self.offering_users:
            offering_user.refresh_from_db()
            self.assertEqual(offering_user.username, "j.smith")

    def test_an_unbacked_account_is_still_editable(self):
        """Provider scope is opt-in; the historical behaviour must survive it."""
        plain_offering = factories.OfferingFactory()
        plain = models.OfferingUser.objects.create(
            offering=plain_offering,
            user=structure_factories.UserFactory(),
            username="solo",
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.OfferingUserFactory.get_url(plain), {"username": "renamed"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plain.refresh_from_db()
        self.assertEqual(plain.username, "renamed")


class ProviderScopeResolutionTest(test.APITestCase):
    """Which scope an offering resolves to, and what overrides what."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.offering = self.fixture.offering

    def test_offering_scope_is_the_default(self):
        self.assertEqual(self.offering.resolve_account_scope(), AccountScopes.OFFERING)
        self.assertFalse(self.offering.uses_provider_accounts)

    def test_the_provider_setting_applies_to_its_offerings(self):
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()
        self.assertTrue(self.offering.uses_provider_accounts)

    def test_an_offering_can_opt_out_of_its_providers_scope(self):
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()
        self.offering.plugin_options = {"account_scope": "offering"}
        self.offering.save()
        self.assertFalse(self.offering.uses_provider_accounts)

    def test_an_unrecognised_override_falls_back_rather_than_guessing(self):
        self.offering.plugin_options = {"account_scope": "nonsense"}
        self.offering.save()
        self.assertEqual(self.offering.resolve_account_scope(), AccountScopes.OFFERING)
