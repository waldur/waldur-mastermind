"""Adopting provider-level accounts, and the username conflicts that block it."""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.enums import AccountScopes, OfferingUserStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class ProviderAccountAdoptionTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.customer = self.provider.customer
        self.offering_a = self.fixture.offering
        self.offering_b = factories.OfferingFactory(customer=self.customer)
        self.user = structure_factories.UserFactory()

    def _account(self, offering, username, **backend_metadata):
        return models.OfferingUser.objects.create(
            offering=offering,
            user=self.user,
            username=username,
            state=OfferingUserStates.OK,
            backend_metadata=backend_metadata or {},
        )

    # -- the report ----------------------------------------------------------

    def test_matching_usernames_are_not_a_conflict(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "jsmith")
        self.assertEqual(utils.provider_username_conflicts(self.provider), [])

    def test_divergent_usernames_are_reported_with_their_evidence(self):
        self._account(self.offering_a, "jsmith", homeDir="/home/jsmith")
        self._account(self.offering_b, "j.smith", homeDir="/home/j.smith")

        conflicts = utils.provider_username_conflicts(self.provider)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["user_uuid"], self.user.uuid.hex)
        names = [c["username"] for c in conflicts[0]["candidates"]]
        self.assertEqual(names, ["j.smith", "jsmith"])
        homes = {
            c["username"]: c["home_directories"] for c in conflicts[0]["candidates"]
        }
        self.assertEqual(homes["jsmith"], ["/home/jsmith"])

    def test_a_users_accounts_at_another_provider_are_not_a_conflict(self):
        other_offering = factories.OfferingFactory()
        self._account(self.offering_a, "jsmith")
        models.OfferingUser.objects.create(
            offering=other_offering, user=self.user, username="somethingelse"
        )
        self.assertEqual(utils.provider_username_conflicts(self.provider), [])

    def test_accounts_without_a_username_are_ignored(self):
        # An account still waiting for a name has nothing to disagree about.
        self._account(self.offering_a, "jsmith")
        models.OfferingUser.objects.create(
            offering=self.offering_b, user=self.user, username=""
        )
        self.assertEqual(utils.provider_username_conflicts(self.provider), [])

    # -- adoption ------------------------------------------------------------

    def test_agreeing_accounts_adopt_without_operator_input(self):
        a = self._account(self.offering_a, "jsmith", uidnumber=9001)
        b = self._account(self.offering_b, "jsmith", uidnumber=9001)

        result = utils.adopt_provider_accounts(self.provider)
        self.assertEqual(result, {"adopted": 1, "backed": 2})

        account = models.ServiceProviderAccount.objects.get(
            service_provider=self.provider, user=self.user
        )
        self.assertEqual(account.username, "jsmith")
        for offering_user in (a, b):
            offering_user.refresh_from_db()
            self.assertEqual(offering_user.service_provider_account, account)

    def test_adoption_is_refused_while_a_conflict_is_unresolved(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "j.smith")

        with self.assertRaises(Exception) as caught:
            utils.adopt_provider_accounts(self.provider)
        self.assertIn("conflicts", str(caught.exception))
        # Nothing half-applied.
        self.assertFalse(models.ServiceProviderAccount.objects.exists())

    def test_a_resolution_picks_the_surviving_username(self):
        a = self._account(self.offering_a, "jsmith", uidnumber=9001)
        b = self._account(self.offering_b, "j.smith", uidnumber=9002)

        utils.adopt_provider_accounts(self.provider, {self.user.uuid.hex: "jsmith"})

        account = models.ServiceProviderAccount.objects.get(
            service_provider=self.provider, user=self.user
        )
        self.assertEqual(account.username, "jsmith")
        # The losing account is renamed onto the survivor rather than left behind.
        for offering_user in (a, b):
            offering_user.refresh_from_db()
            self.assertEqual(offering_user.username, "jsmith")
            self.assertEqual(offering_user.service_provider_account, account)

    def test_adoption_is_idempotent(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "jsmith")
        utils.adopt_provider_accounts(self.provider)
        # Already-backed accounts are not adopted twice.
        self.assertEqual(
            utils.adopt_provider_accounts(self.provider), {"adopted": 0, "backed": 0}
        )
        self.assertEqual(models.ServiceProviderAccount.objects.count(), 1)

    # -- the API guard -------------------------------------------------------

    def test_enabling_provider_scope_is_refused_while_conflicts_remain(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "j.smith")
        self.client.force_authenticate(self.fixture.service_owner)

        response = self.client.patch(
            factories.ServiceProviderFactory.get_url(self.provider),
            {"account_scope": AccountScopes.PROVIDER},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.account_scope, AccountScopes.OFFERING)

    def test_enabling_provider_scope_succeeds_once_they_agree(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "jsmith")
        self.client.force_authenticate(self.fixture.service_owner)

        response = self.client.patch(
            factories.ServiceProviderFactory.get_url(self.provider),
            {"account_scope": AccountScopes.PROVIDER},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.account_scope, AccountScopes.PROVIDER)

    def test_conflicts_endpoint_reports_them(self):
        self._account(self.offering_a, "jsmith")
        self._account(self.offering_b, "j.smith")
        self.client.force_authenticate(self.fixture.service_owner)

        url = factories.ServiceProviderFactory.get_url(
            self.provider, action="username_conflicts"
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(len(response.data[0]["candidates"]), 2)
