"""Creating and releasing provider accounts as users gain and lose access.

The account is the provider's whole directory entry, so it has to outlive the
individual offering associations: losing one offering while still holding another
must not delete the entry the other one depends on.
"""

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models, tasks, utils
from waldur_mastermind.marketplace.enums import AccountScopes, OfferingUserStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class ProviderAccountLifecycleTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.provider = self.fixture.service_provider
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.account_homedir_prefix = "/home/"
        self.provider.save()
        self.offering_a = self.fixture.offering
        self.offering_b = factories.OfferingFactory(customer=self.provider.customer)
        self.user = structure_factories.UserFactory()

    def _accounts(self):
        return models.ServiceProviderAccount.objects.filter(
            service_provider=self.provider, user=self.user
        )

    # -- creation ------------------------------------------------------------

    def test_first_offering_creates_the_provider_account(self):
        account = utils.get_or_create_provider_account(self.user, self.offering_a)
        self.assertIsNotNone(account)
        self.assertEqual(self._accounts().count(), 1)

    def test_second_offering_reuses_it_rather_than_making_another(self):
        """The whole point: one person, one entry, however many offerings."""
        first = utils.get_or_create_provider_account(self.user, self.offering_a)
        second = utils.get_or_create_provider_account(self.user, self.offering_b)
        self.assertEqual(first, second)
        self.assertEqual(self._accounts().count(), 1)

    def test_an_offering_scoped_offering_gets_no_provider_account(self):
        plain = factories.OfferingFactory(customer=self.provider.customer)
        plain.plugin_options = {"account_scope": "offering"}
        plain.save()
        self.assertIsNone(utils.get_or_create_provider_account(self.user, plain))
        self.assertEqual(self._accounts().count(), 0)

    def test_an_offering_whose_customer_is_not_a_provider_falls_back(self):
        # Nothing to hang a provider account on, so the historical per-offering
        # behaviour has to survive rather than the call exploding.
        orphan = factories.OfferingFactory()
        orphan.plugin_options = {"account_scope": "provider"}
        orphan.save()
        self.assertIsNone(utils.get_or_create_provider_account(self.user, orphan))

    # -- release -------------------------------------------------------------

    def _provisioned(self):
        """A provider account that reached OK, as one with a username does."""
        account = utils.get_or_create_provider_account(self.user, self.offering_a)
        account.username = "jsmith"
        account.save()
        account.refresh_from_db()
        return account

    def _back(self, offering, account):
        offering_user = models.OfferingUser.objects.create(
            offering=offering,
            user=self.user,
            service_provider_account=account,
            state=OfferingUserStates.OK,
        )
        offering_user.pull_from_provider_account()
        offering_user.save()
        return offering_user

    def test_losing_one_offering_leaves_the_account_alone(self):
        account = self._provisioned()
        a = self._back(self.offering_a, account)
        self._back(self.offering_b, account)

        a.state = OfferingUserStates.DELETED
        a.save(update_fields=["state"])
        tasks.request_provider_account_deletion_for_user(self.user)

        account.refresh_from_db()
        self.assertEqual(account.state, OfferingUserStates.OK)

    def test_losing_the_last_offering_releases_the_account(self):
        account = self._provisioned()
        for offering in (self.offering_a, self.offering_b):
            offering_user = self._back(offering, account)
            offering_user.state = OfferingUserStates.DELETED
            offering_user.save(update_fields=["state"])

        tasks.request_provider_account_deletion_for_user(self.user)

        account.refresh_from_db()
        self.assertEqual(account.state, OfferingUserStates.DELETION_REQUESTED)

    def test_another_users_accounts_do_not_hold_it_open(self):
        account = self._provisioned()
        gone = self._back(self.offering_a, account)
        gone.state = OfferingUserStates.DELETED
        gone.save(update_fields=["state"])

        # A different user, still active at the same provider.
        other = structure_factories.UserFactory()
        other_account = utils.get_or_create_provider_account(other, self.offering_a)
        other_account.username = "other"
        other_account.save()
        models.OfferingUser.objects.create(
            offering=self.offering_b,
            user=other,
            service_provider_account=other_account,
            state=OfferingUserStates.OK,
        )

        tasks.request_provider_account_deletion_for_user(self.user)

        account.refresh_from_db()
        other_account.refresh_from_db()
        self.assertEqual(account.state, OfferingUserStates.DELETION_REQUESTED)
        self.assertEqual(other_account.state, OfferingUserStates.OK)

    def test_the_fk_refuses_to_orphan_a_backed_offering_account(self):
        """The FK is what makes the two-stage ordering structural, not just intended."""
        from django.db.models import RestrictedError

        account = utils.get_or_create_provider_account(self.user, self.offering_a)
        self._back(self.offering_a, account)
        with self.assertRaises(RestrictedError):
            account.delete()

    def test_deleting_the_user_takes_both_accounts_with_it(self):
        """The reason the FK is RESTRICT rather than PROTECT.

        Both models hang off User with CASCADE, so a user deletion collects the
        provider account and the offering account in the same pass. PROTECT
        refuses that, because it does not ask whether the referencing row is
        itself being deleted; RESTRICT does, and allows it.
        """
        account = utils.get_or_create_provider_account(self.user, self.offering_a)
        offering_user = self._back(self.offering_a, account)

        self.user.delete()

        self.assertFalse(
            models.ServiceProviderAccount.objects.filter(pk=account.pk).exists()
        )
        self.assertFalse(
            models.OfferingUser.objects.filter(pk=offering_user.pk).exists()
        )

    def test_an_unprovisioned_account_is_marked_deleted_not_requested(self):
        """It was never created anywhere, so there is nothing to tear down.

        CREATION_REQUESTED is not a legal source for request_deletion, so this
        path has to end in DELETED or the release raises instead of cleaning up.
        """
        account = utils.get_or_create_provider_account(self.user, self.offering_a)
        self.assertEqual(account.state, OfferingUserStates.CREATION_REQUESTED)
        offering_user = self._back(self.offering_a, account)
        offering_user.state = OfferingUserStates.DELETED
        offering_user.save(update_fields=["state"])

        tasks.request_provider_account_deletion_for_user(self.user)

        account.refresh_from_db()
        self.assertEqual(account.state, OfferingUserStates.DELETED)
