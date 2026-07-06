"""The provider offering serializer exposes whether the offering auto-creates
offering users, so list UIs can gate the GLAuth configuration action."""

from rest_framework import test

from waldur_mastermind.marketplace import serializers
from waldur_mastermind.marketplace.tests import factories


class ProviderOfferingGlauthFlagTest(test.APITestCase):
    def test_getter_reflects_plugin_option(self):
        serializer = serializers.ProviderOfferingSerializer()
        enabled = factories.OfferingFactory(
            plugin_options={"service_provider_can_create_offering_user": True}
        )
        disabled = factories.OfferingFactory(plugin_options={})

        self.assertIs(
            serializer.get_service_provider_can_create_offering_user(enabled), True
        )
        self.assertIs(
            serializer.get_service_provider_can_create_offering_user(disabled),
            False,
        )
