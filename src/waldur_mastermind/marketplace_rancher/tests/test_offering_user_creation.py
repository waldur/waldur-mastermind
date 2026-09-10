"""A Rancher user gets an offering account, backed when the provider owns it.

This path had no coverage at all before, which mattered once it was rerouted
through the shared ``create_offering_user``: a Rancher deployment could have
stopped creating accounts entirely and nothing would have said so.
"""

from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import AccountScopes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_rancher.tests import factories as rancher_factories


class RancherOfferingUserCreationTest(test.APITestCase):
    def setUp(self):
        self.service_settings = rancher_factories.RancherServiceSettingsFactory()
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.service_settings
        )
        self.provider = marketplace_factories.ServiceProviderFactory(
            customer=self.offering.customer
        )

    def create_rancher_user(self):
        return rancher_factories.RancherUserFactory(settings=self.service_settings)

    def get_account(self, rancher_user):
        return marketplace_models.OfferingUser.objects.get(
            offering=self.offering, user=rancher_user.user
        )

    def test_an_account_is_created_with_the_waldur_username(self):
        """Per-offering scope: unchanged from before the shared creator."""
        rancher_user = self.create_rancher_user()

        account = self.get_account(rancher_user)
        self.assertEqual(account.username, rancher_user.user.username)
        self.assertFalse(account.is_provider_backed)

    def test_under_provider_scope_the_account_is_backed(self):
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()

        rancher_user = self.create_rancher_user()

        account = self.get_account(rancher_user)
        self.assertTrue(account.is_provider_backed)
        parent = account.service_provider_account
        assert parent is not None
        self.assertEqual(parent.service_provider, self.provider)

    def test_the_provider_holds_exactly_one_account_for_the_user(self):
        """The point of provider scope: one identity across the provider."""
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()

        rancher_user = self.create_rancher_user()

        self.assertEqual(
            marketplace_models.ServiceProviderAccount.objects.filter(
                service_provider=self.provider, user=rancher_user.user
            ).count(),
            1,
        )
        parent = self.get_account(rancher_user).service_provider_account
        assert parent is not None
        self.assertEqual(parent.user, rancher_user.user)

    def test_no_account_without_a_matching_offering(self):
        """An unmatched settings row must not raise, only skip."""
        orphan_settings = rancher_factories.RancherServiceSettingsFactory()

        rancher_user = rancher_factories.RancherUserFactory(settings=orphan_settings)

        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(
                user=rancher_user.user
            ).exists()
        )
