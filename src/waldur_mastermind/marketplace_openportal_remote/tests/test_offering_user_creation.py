"""OpenPortal Remote association creates an offering account, backed when the provider owns it.

This plugin had no tests touching ``OfferingUser`` at all, which mattered once
the handler was rerouted through the shared ``create_offering_user``: the
account creation could have stopped entirely with nothing to say so.

The handler is driven directly rather than through the signal. The signal
wiring in ``apps.py`` is unchanged; what is worth covering is the offering
lookup and the account creation inside the handler.
"""

from types import SimpleNamespace

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import AccountScopes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openportal_remote import handlers


class OpenPortalRemoteOfferingUserCreationTest(test.APITestCase):
    def setUp(self):
        self.settings = structure_factories.ServiceSettingsFactory()
        self.offering = marketplace_factories.OfferingFactory(scope=self.settings)
        self.provider = marketplace_factories.ServiceProviderFactory(
            customer=self.offering.customer
        )
        self.user = structure_factories.UserFactory()

    def allocation(self, settings=None):
        settings = settings or self.settings
        return SimpleNamespace(
            service_settings=settings, service_settings_id=settings.id
        )

    def create(self, settings=None):
        handlers.create_offering_user_for_openportal_remote_user(
            sender=None, allocation=self.allocation(settings), user=self.user
        )

    def test_an_account_is_created_for_the_user(self):
        self.create()

        self.assertTrue(
            marketplace_models.OfferingUser.objects.filter(
                offering=self.offering, user=self.user
            ).exists()
        )

    def test_under_provider_scope_the_account_is_backed(self):
        self.provider.account_scope = AccountScopes.PROVIDER
        self.provider.save()

        self.create()

        account = marketplace_models.OfferingUser.objects.get(
            offering=self.offering, user=self.user
        )
        self.assertTrue(account.is_provider_backed)

    def test_calling_it_twice_does_not_duplicate(self):
        self.create()
        self.create()

        self.assertEqual(
            marketplace_models.OfferingUser.objects.filter(
                offering=self.offering, user=self.user
            ).count(),
            1,
        )

    def test_an_unmatched_settings_row_is_skipped_not_raised(self):
        self.create(settings=structure_factories.ServiceSettingsFactory())

        self.assertFalse(
            marketplace_models.OfferingUser.objects.filter(user=self.user).exists()
        )
