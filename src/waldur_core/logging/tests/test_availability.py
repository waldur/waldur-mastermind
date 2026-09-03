from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework import status, test

from waldur_core.core.models import Feature
from waldur_core.logging import availability
from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventGroup
from waldur_core.logging.tests.factories import WebHookFactory
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

# Behaviour every Waldur deployment has, so it must survive an empty database.
BASELINE = {
    EventGroup.ACCESS_SUBNETS,
    EventGroup.AUTH,
    EventGroup.CREDITS,
    EventGroup.CUSTOMERS,
    EventGroup.INVOICES,
    EventGroup.PERMISSIONS,
    EventGroup.PROJECTS,
    EventGroup.RESOURCES,
    EventGroup.SSH,
    EventGroup.SUPPORT,
    EventGroup.TERMS_OF_SERVICE,
    EventGroup.USERS,
}

OPENSTACK_GROUPS = {
    group for group in EVENT_GROUP_MAPPING if group.value.startswith("openstack_")
}


class AvailabilityDeclarationTest(TestCase):
    def test_every_group_declares_availability(self):
        self.assertEqual([], availability.get_undeclared_groups())

    def test_openstack_offering_types_are_registered_with_the_marketplace(self):
        """The literals in availability.py must stay real offering types.

        They cannot be imported from mastermind, so a plugin rename would
        otherwise hide every OpenStack group with nothing failing.
        """
        registered = set(manager.backends)
        for offering_type in availability.OPENSTACK_OFFERINGS.types:
            self.assertIn(offering_type, registered)


class AvailableGroupsTest(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_baseline_is_available_on_an_empty_deployment(self):
        available = set(availability.get_available_group_keys())
        for group in BASELINE:
            self.assertIn(group.value, available)

    def test_openstack_groups_are_hidden_without_openstack_offerings(self):
        available = set(availability.get_available_group_keys())
        for group in OPENSTACK_GROUPS:
            self.assertNotIn(group.value, available)

    def test_openstack_groups_appear_once_an_offering_exists(self):
        marketplace_factories.OfferingFactory(type="OpenStack.Tenant")
        availability.invalidate_cache()

        available = set(availability.get_available_group_keys())
        for group in OPENSTACK_GROUPS:
            self.assertIn(group.value, available)

    def test_offering_of_another_type_does_not_reveal_openstack_groups(self):
        marketplace_factories.OfferingFactory(type="Marketplace.Script")
        availability.invalidate_cache()

        available = set(availability.get_available_group_keys())
        self.assertNotIn(EventGroup.OPENSTACK_RESOURCES.value, available)

    def test_feature_gated_group_follows_its_flag(self):
        self.assertNotIn(
            EventGroup.ONBOARDING.value, availability.get_available_group_keys()
        )

        Feature.objects.create(key="customer.show_onboarding", value=True)
        availability.invalidate_cache()

        self.assertIn(
            EventGroup.ONBOARDING.value, availability.get_available_group_keys()
        )

    def test_result_is_cached_between_calls(self):
        availability.get_available_group_keys()
        with mock.patch.object(
            availability.OfferingTypes, "is_available"
        ) as is_available:
            availability.get_available_group_keys()
        is_available.assert_not_called()

    def test_creating_an_offering_invalidates_the_cache(self):
        self.assertNotIn(
            EventGroup.OPENSTACK_RESOURCES.value,
            availability.get_available_group_keys(),
        )

        marketplace_factories.OfferingFactory(type="OpenStack.Tenant")

        self.assertIn(
            EventGroup.OPENSTACK_RESOURCES.value,
            availability.get_available_group_keys(),
        )

    def test_a_degraded_answer_is_not_cached(self):
        """A transient fault must not pin "advertise everything" for the whole
        timeout after it clears."""
        with mock.patch.object(
            availability.OfferingTypes,
            "is_available",
            side_effect=RuntimeError("no such table"),
        ):
            availability.get_available_group_keys()

        self.assertIsNone(cache.get(availability.CACHE_KEY))
        self.assertNotIn(
            EventGroup.OPENSTACK_RESOURCES.value,
            availability.get_available_group_keys(),
        )

    def test_toggling_a_feature_invalidates_the_cache(self):
        self.assertNotIn(
            EventGroup.ONBOARDING.value, availability.get_available_group_keys()
        )

        Feature.objects.create(key="customer.show_onboarding", value=True)

        self.assertIn(
            EventGroup.ONBOARDING.value, availability.get_available_group_keys()
        )

    def test_a_failing_declaration_does_not_hide_the_group(self):
        with mock.patch.object(
            availability.OfferingTypes,
            "is_available",
            side_effect=RuntimeError("no such table"),
        ):
            available = availability.get_available_group_keys()
        for group in OPENSTACK_GROUPS:
            self.assertIn(group.value, available)

    def test_available_groups_carry_their_event_types(self):
        groups = availability.get_available_event_groups()
        self.assertIn("auth", groups)
        self.assertIn("auth_logged_out", groups["auth"])
        self.assertNotIn("openstack_resources", groups)


class EventGroupsEndpointTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.user)
        self.url = "/api/events/event_groups/"

    def tearDown(self):
        cache.clear()

    def test_endpoint_omits_groups_the_deployment_cannot_emit(self):
        response = self.client.get(self.url)

        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertIn("resources", response.data)
        self.assertNotIn("openstack_resources", response.data)

    def test_response_shape_is_unchanged(self):
        response = self.client.get(self.url)

        for key, event_types in response.data.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(event_types, list)
            for event_type in event_types:
                self.assertIsInstance(event_type, str)

    def test_hook_can_still_subscribe_to_a_group_that_is_not_advertised(self):
        """Filtering is discovery-only: it must never invalidate a subscription."""
        author = structure_factories.UserFactory()
        self.client.force_authenticate(user=author)

        response = self.client.post(
            WebHookFactory.get_list_url(),
            data={
                "event_groups": ["openstack_resources"],
                "destination_url": "http://example.com/",
            },
        )

        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        expected = {
            event.value for event in EVENT_GROUP_MAPPING[EventGroup.OPENSTACK_RESOURCES]
        }
        self.assertTrue(expected.issubset(set(response.data["event_types"])))
