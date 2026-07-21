from freezegun import freeze_time
from rest_framework import test

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventGroup
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.utils import get_current_month, get_current_year

from . import factories


class EventsStatsGetTest(test.APITestCase):
    def setUp(self) -> None:
        with freeze_time("2021-01-01"):
            self.user = structure_factories.UserFactory(is_staff=True)

        with freeze_time("2021-02-01"):
            self.user2 = structure_factories.UserFactory(is_staff=True)

            event = factories.EventFactory()
            factories.FeedFactory(scope=self.user, event=event)

        self.client.force_login(self.user)
        self.url = factories.EventFactory.get_stats_list_url()

    def test_get_events_stats(self):
        response = self.client.get(
            self.url, {"scope": structure_factories.UserFactory.get_url(self.user)}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(3, len(response.data))
        self.assertEqual(
            [
                {"year": get_current_year(), "month": get_current_month(), "count": 1},
                {"year": 2021, "month": 2, "count": 1},
                {"year": 2021, "month": 1, "count": 2},
            ],
            response.data,
        )

    def test_events_stats_filter_by_event_type(self):
        response = self.client.get(self.url, {"event_type": "user_creation_succeeded"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, len(response.data))
        self.assertEqual(
            [
                {"year": 2021, "month": 2, "count": 1},
                {"year": 2021, "month": 1, "count": 1},
            ],
            response.data,
        )

    def test_unauthorized_user_can_not_get_stats(self):
        self.client.logout()

        response = self.client.get(
            self.url, {"scope": structure_factories.UserFactory.get_url(self.user)}
        )

        self.assertEqual(401, response.status_code)


class EventsFilteringTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.user)
        self.url = factories.EventFactory.get_list_url()

        # Create events for different event groups
        self.components_event = factories.EventFactory(
            event_type="marketplace_plan_created"
        )
        self.resources_event = factories.EventFactory(
            event_type="marketplace_resource_create_succeeded"
        )

    def test_filter_events_by_feature(self):
        """
        Test filtering events by feature.
        """
        response = self.client.get(self.url, {"feature": "offering_accounting"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["event_type"], "marketplace_plan_created")

    def test_filter_events_by_multiple_features(self):
        """
        Test filtering events by multiple features.
        """
        response = self.client.get(
            self.url, {"feature": ["offering_accounting", "resources"]}
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 2)
        event_types = [event["event_type"] for event in response.data]
        self.assertEqual(
            sorted(event_types),
            sorted(
                [
                    "marketplace_plan_created",
                    "marketplace_resource_create_succeeded",
                ]
            ),
        )

    def test_filter_events_by_nonexistent_feature(self):
        """
        Test filtering events by nonexistent feature.
        """
        response = self.client.get(self.url, {"feature": "whatever"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 0)


class RelatedUserUuidFilterTest(test.APITestCase):
    """Tests for related_user_uuid: OR of Feed scope, actor, and affected user."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.target_user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.staff)
        self.url = factories.EventFactory.get_list_url()
        self.target_uuid = self.target_user.uuid.hex

        self.feed_event = factories.EventFactory(
            event_type="user_update_succeeded",
            context={"affected_user_uuid": "unrelated"},
        )
        factories.FeedFactory(scope=self.target_user, event=self.feed_event)

        self.actor_event = factories.EventFactory(
            event_type="marketplace_resource_create_succeeded",
            context={"user_uuid": self.target_uuid},
        )

        self.affected_event = factories.EventFactory(
            event_type="user_password_updated_by_staff",
            context={
                "user_uuid": self.staff.uuid.hex,
                "affected_user_uuid": self.target_uuid,
            },
        )

        self.unrelated_event = factories.EventFactory(
            event_type="marketplace_resource_update_succeeded",
            context={"user_uuid": self.other_user.uuid.hex},
        )
        factories.FeedFactory(scope=self.other_user, event=self.unrelated_event)

    def _messages(self, response):
        return {event["message"] for event in response.data}

    def test_returns_union_of_feed_actor_and_affected(self):
        response = self.client.get(self.url, {"related_user_uuid": self.target_uuid})

        self.assertEqual(200, response.status_code)
        messages = self._messages(response)
        self.assertIn(self.feed_event.message, messages)
        self.assertIn(self.actor_event.message, messages)
        self.assertIn(self.affected_event.message, messages)
        self.assertNotIn(self.unrelated_event.message, messages)

    def test_does_not_duplicate_event_matching_multiple_criteria(self):
        overlap = factories.EventFactory(
            event_type="user_activated",
            context={
                "user_uuid": self.target_uuid,
                "affected_user_uuid": self.target_uuid,
            },
        )
        factories.FeedFactory(scope=self.target_user, event=overlap)

        response = self.client.get(self.url, {"related_user_uuid": self.target_uuid})

        self.assertEqual(200, response.status_code)
        matching = [e for e in response.data if e["message"] == overlap.message]
        self.assertEqual(len(matching), 1)

    def test_invalid_uuid_returns_empty(self):
        response = self.client.get(self.url, {"related_user_uuid": "not-a-uuid"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 0)

    def test_unknown_user_returns_empty(self):
        response = self.client.get(
            self.url, {"related_user_uuid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 0)

    def test_support_user_can_filter(self):
        support = structure_factories.UserFactory(is_support=True)
        self.client.force_authenticate(user=support)

        response = self.client.get(self.url, {"related_user_uuid": self.target_uuid})

        self.assertEqual(200, response.status_code)
        messages = self._messages(response)
        self.assertIn(self.feed_event.message, messages)
        self.assertIn(self.actor_event.message, messages)
        self.assertIn(self.affected_event.message, messages)

    def test_regular_user_cannot_see_peer_related_events(self):
        """Visible peers must not get another user's cross-scope audit trail."""
        regular = structure_factories.UserFactory()
        self.client.force_authenticate(user=regular)

        response = self.client.get(self.url, {"related_user_uuid": self.target_uuid})

        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 0)

    def test_regular_user_can_see_own_related_events(self):
        self.client.force_authenticate(user=self.target_user)

        response = self.client.get(self.url, {"related_user_uuid": self.target_uuid})

        self.assertEqual(200, response.status_code)
        messages = self._messages(response)
        self.assertIn(self.feed_event.message, messages)
        self.assertIn(self.actor_event.message, messages)
        self.assertIn(self.affected_event.message, messages)

    def test_broader_than_user_uuid_alone(self):
        """user_uuid only matches actor; related_user_uuid also matches feed/affected."""
        by_actor = self.client.get(self.url, {"user_uuid": self.target_uuid})
        by_related = self.client.get(self.url, {"related_user_uuid": self.target_uuid})

        self.assertEqual(200, by_actor.status_code)
        self.assertEqual(200, by_related.status_code)
        actor_messages = self._messages(by_actor)
        related_messages = self._messages(by_related)
        self.assertIn(self.actor_event.message, actor_messages)
        self.assertNotIn(self.feed_event.message, actor_messages)
        self.assertNotIn(self.affected_event.message, actor_messages)
        self.assertTrue(related_messages.issuperset(actor_messages))
        self.assertIn(self.feed_event.message, related_messages)
        self.assertIn(self.affected_event.message, related_messages)
        self.assertGreater(len(related_messages), len(actor_messages))


class EventGroupsAPITest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.user)
        self.url = "/api/events/event_groups/"

    def test_event_groups_endpoint_returns_json_with_string_keys(self):
        """
        Test that event_groups API endpoint returns JSON with string keys,
        not enum objects, to ensure ORJSON renderer compatibility.
        """
        response = self.client.get(self.url)

        self.assertEqual(200, response.status_code)

        # Verify response is valid JSON with string keys
        self.assertIsInstance(response.data, dict)

        # All keys should be strings
        for key in response.data.keys():
            self.assertIsInstance(
                key, str, f"Key '{key}' should be a string, not {type(key)}"
            )

        # All values should be lists of strings
        for group_name, event_types in response.data.items():
            self.assertIsInstance(
                event_types, list, f"Value for '{group_name}' should be a list"
            )
            for event_type in event_types:
                self.assertIsInstance(
                    event_type, str, f"Event type '{event_type}' should be a string"
                )

        # Verify we have expected groups (at least a few common ones)
        self.assertIn("auth", response.data)
        self.assertIn("users", response.data)
        self.assertIn("resources", response.data)

        # Verify auth group has expected events
        auth_events = response.data["auth"]
        self.assertIn("auth_logged_in_with_username", auth_events)
        self.assertIn("auth_logged_out", auth_events)

    def test_event_groups_endpoint_matches_get_event_groups_function(self):
        """
        Test that the API endpoint returns the same data as get_event_groups() function.
        """
        response = self.client.get(self.url)
        expected_data = event_logger.get_event_groups()

        self.assertEqual(response.data, expected_data)


class ExpandEventGroupsTest(test.APITestCase):
    """Test expand_event_groups function with different input types"""

    def test_expand_event_groups_with_string_input(self):
        """Test expand_event_groups function handles string group names"""
        # Test with string input (as would come from API query params)
        result = event_logger.expand_event_groups(["auth", "users"])

        self.assertIsInstance(result, list)
        self.assertTrue(len(result) > 0)

        # Should contain auth events
        self.assertIn("auth_logged_in_with_username", result)
        self.assertIn("auth_logged_out", result)

        # Should contain user events
        self.assertIn("user_creation_succeeded", result)

    def test_expand_event_groups_with_enum_input(self):
        """Test expand_event_groups function handles EventGroup enum objects"""
        # Test with enum input (for backward compatibility)
        result = event_logger.expand_event_groups([EventGroup.AUTH, EventGroup.USERS])

        self.assertIsInstance(result, list)
        self.assertTrue(len(result) > 0)

        # Should contain auth events
        self.assertIn("auth_logged_in_with_username", result)
        self.assertIn("auth_logged_out", result)

        # Should contain user events
        self.assertIn("user_creation_succeeded", result)

    def test_expand_event_groups_with_mixed_input(self):
        """Test expand_event_groups function handles mixed string and enum input"""
        result = event_logger.expand_event_groups(["auth", EventGroup.USERS])

        self.assertIsInstance(result, list)
        self.assertTrue(len(result) > 0)

        # Should contain events from both groups
        self.assertIn("auth_logged_in_with_username", result)
        self.assertIn("user_creation_succeeded", result)

    def test_expand_event_groups_with_invalid_string(self):
        """Test expand_event_groups function handles invalid group names gracefully"""
        result = event_logger.expand_event_groups(["invalid_group_name"])

        # Should return empty list for invalid groups
        self.assertEqual(result, [])

    def test_expand_event_groups_returns_sorted_list(self):
        """Test that expand_event_groups returns a sorted list"""
        result = event_logger.expand_event_groups(["auth"])

        self.assertEqual(result, sorted(result))
