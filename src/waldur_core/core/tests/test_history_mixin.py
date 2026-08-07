import json
from datetime import timedelta

import reversion
from django.utils import timezone
from rest_framework import status, test
from reversion.models import Version

from waldur_core.structure.tests import factories, fixtures


class HistoryViewSetMixinTest(test.APITestCase):
    """Test the HistoryViewSetMixin using CustomerViewSet."""

    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer

    def _get_history_url(self):
        return factories.CustomerFactory.get_url(self.customer, "history")

    def _get_history_at_url(self):
        return factories.CustomerFactory.get_url(self.customer, "history/at")


class HistoryEndpointAccessTest(HistoryViewSetMixinTest):
    """Test access controls for history endpoint."""

    def test_staff_user_can_access_history(self):
        """Staff user should be able to access history."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_user_can_access_history(self):
        """Support user should be able to access history."""
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_cannot_access_history(self):
        """Customer owner should not be able to access history."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_regular_user_cannot_access_history(self):
        """Regular user should not be able to access history."""
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_user_cannot_access_history(self):
        """Unauthenticated user should not be able to access history."""
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class HistoryEndpointTest(HistoryViewSetMixinTest):
    """Test the history endpoint functionality."""

    def test_history_returns_initial_revision_for_new_object(self):
        """New object should have an initial revision from creation."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["revision_comment"], "Initial version")

    def test_history_recorded_on_object_update(self):
        """Updating an object should create a history entry."""
        self.client.force_authenticate(self.fixture.staff)

        # Create a version
        with reversion.create_revision():
            self.customer.name = "Updated Name"
            self.customer.save()
            reversion.set_user(self.fixture.staff)
            reversion.set_comment("Test update")

        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)  # initial + update
        self.assertIn("revision_date", response.data[0])
        self.assertIn("revision_user", response.data[0])
        self.assertIn("serialized_data", response.data[0])
        self.assertIn("revision_comment", response.data[0])

    def test_history_includes_user_info(self):
        """History should include user information."""
        self.client.force_authenticate(self.fixture.staff)

        with reversion.create_revision():
            self.customer.name = "Updated Name"
            self.customer.save()
            reversion.set_user(self.fixture.staff)

        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_info = response.data[0]["revision_user"]
        self.assertEqual(user_info["uuid"], str(self.fixture.staff.uuid))
        self.assertEqual(user_info["username"], self.fixture.staff.username)

    def test_history_returns_versions_in_reverse_chronological_order(self):
        """History should return versions newest first."""
        self.client.force_authenticate(self.fixture.staff)

        # Create multiple versions
        for i in range(3):
            with reversion.create_revision():
                self.customer.name = f"Name v{i + 1}"
                self.customer.save()
                reversion.set_user(self.fixture.staff)
                reversion.set_comment(f"Version {i + 1}")

        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 4)  # initial + 3 updates

        # Verify reverse chronological order (newest first)
        comments = [v["revision_comment"] for v in response.data]
        self.assertEqual(
            comments,
            ["Version 3", "Version 2", "Version 1", "Initial version"],
        )


class HistoryFilteringTest(HistoryViewSetMixinTest):
    """Test history filtering functionality."""

    def _create_versions_with_timestamps(self):
        """Create versions with known timestamps for testing."""
        self.time_before_all = timezone.now()

        # First version
        with reversion.create_revision():
            self.customer.name = "Name v1"
            self.customer.save()
            reversion.set_user(self.fixture.staff)

        self.time_after_first = timezone.now()

        # Second version
        with reversion.create_revision():
            self.customer.name = "Name v2"
            self.customer.save()
            reversion.set_user(self.fixture.staff)

        self.time_after_second = timezone.now()

        # Third version
        with reversion.create_revision():
            self.customer.name = "Name v3"
            self.customer.save()
            reversion.set_user(self.fixture.staff)

        self.time_after_all = timezone.now()

    def test_filter_by_created_before(self):
        """Filter versions created before a timestamp."""
        self._create_versions_with_timestamps()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self._get_history_url(),
            {"created_before": self.time_after_second.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return initial + first two versions (created before time_after_second)
        self.assertEqual(len(response.data), 3)

    def test_filter_by_created_after(self):
        """Filter versions created after a timestamp."""
        self._create_versions_with_timestamps()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self._get_history_url(),
            {"created_after": self.time_after_first.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return last two versions (created after time_after_first)
        self.assertEqual(len(response.data), 2)

    def test_filter_with_both_created_before_and_after(self):
        """Filter versions with both before and after timestamps."""
        self._create_versions_with_timestamps()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self._get_history_url(),
            {
                "created_after": self.time_after_first.isoformat(),
                "created_before": self.time_after_all.isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return second and third versions
        self.assertEqual(len(response.data), 2)

    def test_invalid_created_before_format(self):
        """Invalid created_before timestamp should return 400."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self._get_history_url(),
            {"created_before": "invalid-date"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_created_after_format(self):
        """Invalid created_after timestamp should return 400."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self._get_history_url(),
            {"created_after": "invalid-date"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class HistoryAtEndpointAccessTest(HistoryViewSetMixinTest):
    """Test access controls for history_at endpoint."""

    def test_staff_user_can_access_history_at(self):
        """Staff user should be able to access history_at."""
        self.client.force_authenticate(self.fixture.staff)

        # Create a version first
        with reversion.create_revision():
            self.customer.name = "Updated"
            self.customer.save()
            reversion.set_user(self.fixture.staff)

        response = self.client.get(
            self._get_history_at_url(),
            {"timestamp": timezone.now().isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_user_can_access_history_at(self):
        """Support user should be able to access history_at."""
        self.client.force_authenticate(self.fixture.global_support)

        # Create a version first
        with reversion.create_revision():
            self.customer.name = "Updated"
            self.customer.save()
            reversion.set_user(self.fixture.staff)

        response = self.client.get(
            self._get_history_at_url(),
            {"timestamp": timezone.now().isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_cannot_access_history_at(self):
        """Customer owner should not be able to access history_at."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            self._get_history_at_url(),
            {"timestamp": timezone.now().isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class HistoryAtEndpointTest(HistoryViewSetMixinTest):
    """Test the history_at endpoint functionality."""

    def test_history_at_returns_correct_version_for_timestamp(self):
        """history_at endpoint should return correct version for given timestamp."""
        self.client.force_authenticate(self.fixture.staff)

        # Create initial version
        with reversion.create_revision():
            self.customer.name = "Name v1"
            self.customer.save()
            reversion.set_user(self.fixture.staff)
            reversion.set_comment("First version")

        first_version_time = timezone.now()

        # Wait a moment and create second version
        with reversion.create_revision():
            self.customer.name = "Name v2"
            self.customer.save()
            reversion.set_user(self.fixture.staff)
            reversion.set_comment("Second version")

        # Query for state at first version time
        response = self.client.get(
            self._get_history_at_url(),
            {"timestamp": first_version_time.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["serialized_data"]["name"], "Name v1")

    def test_history_at_returns_404_for_timestamp_before_any_version(self):
        """history_at should return 404 if no version exists before timestamp."""
        self.client.force_authenticate(self.fixture.staff)

        # Query for state before any versions exist
        past_time = timezone.now() - timedelta(days=365)
        response = self.client.get(
            self._get_history_at_url(),
            {"timestamp": past_time.isoformat()},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_history_at_requires_timestamp_parameter(self):
        """history_at should require timestamp parameter."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self._get_history_at_url())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_history_at_validates_timestamp_format(self):
        """history_at should validate timestamp format."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self._get_history_at_url(),
            {"timestamp": "invalid-date"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_history_at_includes_queried_at_in_response(self):
        """history_at response should include queried_at field."""
        self.client.force_authenticate(self.fixture.staff)

        # Create a version
        with reversion.create_revision():
            self.customer.name = "Updated"
            self.customer.save()
            reversion.set_user(self.fixture.staff)

        timestamp = timezone.now().isoformat()
        response = self.client.get(
            self._get_history_at_url(),
            {"timestamp": timestamp},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["queried_at"], timestamp)


class HistoryPaginationTest(HistoryViewSetMixinTest):
    """Test pagination for history endpoint."""

    def test_history_is_paginated(self):
        """History endpoint should support pagination."""
        self.client.force_authenticate(self.fixture.staff)

        # Create multiple versions
        for i in range(15):
            with reversion.create_revision():
                self.customer.name = f"Name v{i + 1}"
                self.customer.save()
                reversion.set_user(self.fixture.staff)

        # Default page size is typically 10
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Waldur uses LinkHeaderPagination which returns data as a list
        # and puts count in the X-Result-Count header
        self.assertIsInstance(response.data, list)
        # First page should have 10 items (default page size)
        self.assertEqual(len(response.data), 10)
        # Total count should be in the header
        self.assertEqual(response["X-Result-Count"], "16")  # initial + 15 updates


class VersionPayloadTest(test.APITestCase):
    """The history payload is a raw model snapshot, so it needs its own
    guards against leaking fields the model's own serializer withholds."""

    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.user = factories.UserFactory()

    def get_history(self, user=None):
        self.client.force_authenticate(user or self.fixture.staff)
        response = self.client.get(factories.UserFactory.get_url(self.user, "history"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_password_hash_is_not_exposed(self):
        self.user.set_password("correct horse battery staple")
        self.user.save(update_fields=["password"])
        password_hash = self.user.password
        self.assertTrue(password_hash)
        with reversion.create_revision():
            reversion.add_to_revision(self.user)

        history = self.get_history()

        self.assertTrue(history)
        for version in history:
            self.assertNotIn("password", version["serialized_data"])
        self.assertNotIn(password_hash, json.dumps(history, default=str))

    def test_fields_added_after_the_snapshot_use_the_model_default(self):
        """Otherwise every field added since reads as a change in a diff."""
        version = Version.objects.get_for_object(self.user).first()
        payload = json.loads(version.serialized_data)
        del payload[0]["fields"]["job_title"]
        version.serialized_data = json.dumps(payload)
        version.save()

        data = self.get_history()[0]["serialized_data"]

        self.assertEqual(data["job_title"], "")

    def test_recorded_values_are_left_alone(self):
        self.user.job_title = "Lord Commander"
        self.user.save(update_fields=["job_title"])
        with reversion.create_revision():
            reversion.add_to_revision(self.user)

        data = self.get_history()[0]["serialized_data"]

        self.assertEqual(data["job_title"], "Lord Commander")
