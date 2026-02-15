"""Tests for User attribute update events published via STOMP."""

import datetime
import json
from unittest import mock

from django.test import TestCase

from waldur_core.logging import enums as logging_enums
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.handlers import (
    _build_filtered_user_attributes,
    _serialize_user_field,
)
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OfferingUserFactory,
)


class TestSerializeUserField(TestCase):
    def test_handles_none(self):
        self.assertIsNone(_serialize_user_field(None))

    def test_handles_date(self):
        d = datetime.date(1990, 5, 15)
        self.assertEqual(_serialize_user_field(d), "1990-05-15")

    def test_handles_datetime(self):
        dt = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        self.assertEqual(_serialize_user_field(dt), "2024-01-01T12:00:00+00:00")

    def test_handles_string(self):
        self.assertEqual(_serialize_user_field("hello"), "hello")

    def test_handles_json_list(self):
        val = ["urn:assurance:IAP/low", "urn:assurance:IAP/medium"]
        self.assertEqual(_serialize_user_field(val), val)


class TestBuildFilteredUserAttributes(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.user = self.fixture.user
        self.user.first_name = "Alice"
        self.user.last_name = "Smith"
        self.user.email = "alice@example.com"
        self.user.phone_number = "+123456"
        self.user.save()

    def test_filters_by_exposed_attributes(self):
        attrs = _build_filtered_user_attributes(self.user, {"full_name", "email"})
        self.assertIn("first_name", attrs)
        self.assertIn("last_name", attrs)
        self.assertIn("email", attrs)
        self.assertNotIn("phone_number", attrs)

    def test_skips_empty_values(self):
        self.user.organization = ""
        self.user.save()
        attrs = _build_filtered_user_attributes(self.user, {"organization"})
        self.assertNotIn("organization", attrs)

    def test_skips_none_values(self):
        self.user.birth_date = None
        self.user.save()
        attrs = _build_filtered_user_attributes(self.user, {"birth_date"})
        self.assertNotIn("birth_date", attrs)

    def test_skips_empty_list(self):
        self.user.nationalities = []
        self.user.save()
        attrs = _build_filtered_user_attributes(self.user, {"nationalities"})
        self.assertNotIn("nationalities", attrs)

    def test_serializes_date_fields(self):
        self.user.birth_date = datetime.date(1990, 5, 15)
        self.user.save()
        attrs = _build_filtered_user_attributes(self.user, {"birth_date"})
        self.assertEqual(attrs["birth_date"], "1990-05-15")

    def test_passes_through_json_fields(self):
        self.user.nationalities = ["FI", "DE"]
        self.user.save()
        attrs = _build_filtered_user_attributes(self.user, {"nationalities"})
        self.assertEqual(attrs["nationalities"], ["FI", "DE"])


def _setup_event_subscription(user, offering):
    """Create event subscription and queue required for messages to be sent."""
    event_subscription = logging_factories.EventSubscriptionFactory(
        user=user,
        observable_objects=[
            {"object_type": logging_enums.ObservableObjectType.OFFERING_USER.value}
        ],
    )
    logging_factories.EventSubscriptionQueueFactory(
        event_subscription=event_subscription,
        offering_uuid=offering.uuid,
        object_type=logging_enums.ObservableObjectType.OFFERING_USER.value,
    )
    return event_subscription


class TestUserAttributeUpdateMessage(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = OfferingFactory(customer=self.fixture.customer)
        self.user = self.fixture.user
        self.user.first_name = "Alice"
        self.user.last_name = "Smith"
        self.user.email = "alice@example.com"
        self.user.save()

        self.offering_user = OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username="test-user",
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_full_name=True,
            expose_email=True,
        )
        _setup_event_subscription(self.fixture.staff, self.offering)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_user_attribute_change_sends_event(self, mock_publish):
        self.user.first_name = "Bob"
        self.user.save()

        mock_publish.assert_called_once()
        message = mock_publish.call_args[0][0][0]
        payload = json.loads(message["payload"])

        self.assertEqual(payload["action"], "attribute_update")
        self.assertEqual(payload["offering_user_uuid"], self.offering_user.uuid.hex)
        self.assertEqual(payload["user_uuid"], self.user.uuid.hex)
        self.assertEqual(payload["username"], "test-user")
        self.assertIn("first_name", payload["changed_attributes"])
        self.assertEqual(payload["attributes"]["first_name"], "Bob")
        # last_name is exposed via full_name gate and non-empty
        self.assertIn("last_name", payload["attributes"])

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_user_attribute_change_filters_by_config(self, mock_publish):
        """Offering with expose_full_name=False: changing first_name sends no event."""
        config = models.OfferingUserAttributeConfig.objects.get(offering=self.offering)
        config.expose_full_name = False
        config.save()

        self.user.first_name = "Bob"
        self.user.save()

        mock_publish.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_user_attribute_change_fans_out_per_offering(self, mock_publish):
        """User in 2 offerings with different configs gets different payloads."""
        offering2 = OfferingFactory(customer=self.fixture.customer)
        OfferingUserFactory(
            offering=offering2,
            user=self.user,
            username="test-user-2",
        )
        models.OfferingUserAttributeConfig.objects.create(
            offering=offering2,
            expose_full_name=False,
            expose_email=True,
        )
        _setup_event_subscription(self.fixture.staff, offering2)

        self.user.first_name = "Bob"
        self.user.email = "bob@example.com"
        self.user.save()

        # Should be called twice: once per offering
        self.assertEqual(mock_publish.call_count, 2)

        payloads = []
        for call in mock_publish.call_args_list:
            message = call[0][0][0]
            payloads.append(json.loads(message["payload"]))

        # Find payloads by offering_user_uuid
        payload_by_ou = {p["offering_user_uuid"]: p for p in payloads}

        # Offering 1: expose_full_name=True, expose_email=True
        p1 = payload_by_ou[self.offering_user.uuid.hex]
        self.assertIn("first_name", p1["changed_attributes"])
        self.assertIn("email", p1["changed_attributes"])
        self.assertIn("first_name", p1["attributes"])

        # Offering 2: expose_full_name=False, expose_email=True
        ou2 = models.OfferingUser.objects.get(offering=offering2, user=self.user)
        p2 = payload_by_ou[ou2.uuid.hex]
        self.assertIn("email", p2["changed_attributes"])
        self.assertNotIn("first_name", p2["changed_attributes"])
        self.assertNotIn("first_name", p2["attributes"])

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_user_attribute_change_skips_non_profile_fields(self, mock_publish):
        self.user.is_staff = True
        self.user.save()

        mock_publish.assert_not_called()

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_user_create_does_not_trigger_attribute_event(self, mock_publish):
        """The handler should skip when created=True (new user)."""
        from waldur_core.structure.tests.factories import UserFactory

        UserFactory(first_name="NewUser")

        # The create handler is for OfferingUser, not User.
        # The attribute update handler should not fire for new users.
        # Note: mock_publish may be called for other signals, but not
        # for attribute_update action from our handler.
        for call in mock_publish.call_args_list:
            message = call[0][0][0]
            payload = json.loads(message["payload"])
            self.assertNotEqual(payload.get("action"), "attribute_update")


class TestOfferingUserCreateIncludesAttributes(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = OfferingFactory(customer=self.fixture.customer)
        self.user = self.fixture.user
        self.user.first_name = "Alice"
        self.user.last_name = "Smith"
        self.user.email = "alice@example.com"
        self.user.save()

        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering,
            expose_full_name=True,
            expose_email=True,
        )
        _setup_event_subscription(self.fixture.staff, self.offering)

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_create_includes_attributes(self, mock_publish):
        offering_user = OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username="new-user",
        )

        mock_publish.assert_called_once()
        message = mock_publish.call_args[0][0][0]
        payload = json.loads(message["payload"])

        self.assertEqual(payload["action"], "create")
        self.assertEqual(payload["offering_user_uuid"], offering_user.uuid.hex)
        self.assertIn("attributes", payload)
        self.assertEqual(payload["attributes"]["first_name"], "Alice")
        self.assertEqual(payload["attributes"]["last_name"], "Smith")
        self.assertEqual(payload["attributes"]["email"], "alice@example.com")

    @mock.patch("waldur_core.logging.tasks.publish_messages.delay")
    def test_offering_user_create_filters_attributes_by_config(self, mock_publish):
        """Create event should only include attributes allowed by config."""
        config = models.OfferingUserAttributeConfig.objects.get(offering=self.offering)
        config.expose_full_name = False
        config.save()

        # Refresh offering to clear cached reverse relation
        self.offering.refresh_from_db()

        OfferingUserFactory(
            offering=self.offering,
            user=self.user,
            username="new-user",
        )

        mock_publish.assert_called_once()
        message = mock_publish.call_args[0][0][0]
        payload = json.loads(message["payload"])

        self.assertEqual(payload["action"], "create")
        self.assertIn("attributes", payload)
        self.assertNotIn("first_name", payload["attributes"])
        self.assertNotIn("last_name", payload["attributes"])
        self.assertIn("email", payload["attributes"])
