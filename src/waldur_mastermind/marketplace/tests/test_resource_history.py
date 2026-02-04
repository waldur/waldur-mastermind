import unittest
from datetime import timedelta

import reversion
from django.utils import timezone
from rest_framework import status, test
from reversion.models import Version

from waldur_core.core.views import CreateReversionMixin
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


class ResourceReversionRegistrationTest(test.APITransactionTestCase):
    """Test that Resource model is registered with django-reversion."""

    def test_resource_is_registered_with_reversion(self):
        """Verify Resource model is registered with reversion."""
        self.assertTrue(reversion.is_registered(models.Resource))

    def test_resource_version_contains_expected_fields(self):
        """Verify versions contain the expected tracked fields when saved."""
        from waldur_mastermind.marketplace.tests import factories

        resource = factories.ResourceFactory()

        with reversion.create_revision():
            resource.name = "Test Name"
            resource.save()

        version = Version.objects.get_for_object(resource).first()
        self.assertIsNotNone(version)

        # Check serialized data contains expected fields
        import json

        data = json.loads(version.serialized_data)[0]["fields"]
        expected_fields = [
            "name",
            "description",
            "slug",
            "state",
            "limits",
            "attributes",
            "options",
            "cost",
            "end_date",
            "downscaled",
            "restrict_member_access",
            "paused",
        ]
        for field in expected_fields:
            self.assertIn(field, data)


class ResourceHistoryEndpointTest(test.APITransactionTestCase):
    """Test resource history endpoint."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

    def _get_history_url(self):
        return factories.ResourceFactory.get_url(self.resource, "history")

    def _get_history_at_url(self):
        return factories.ResourceFactory.get_url(self.resource, "history/at")

    def test_history_returns_initial_revision_for_new_resource(self):
        """New resource should have an initial revision from creation."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["revision_comment"], "Initial version")

    def test_history_recorded_on_resource_update(self):
        """Updating a resource should create a history entry."""
        self.client.force_authenticate(self.fixture.staff)

        # Update resource name using the update endpoint
        url = factories.ResourceFactory.get_url(self.resource)
        self.client.patch(url, {"name": "Updated Name"})

        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)  # initial + update
        self.assertIn("revision_date", response.data[0])
        self.assertIn("revision_user", response.data[0])
        self.assertIn("serialized_data", response.data[0])

    def test_staff_user_can_access_history(self):
        """Staff user should be able to access resource history."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_user_can_access_history(self):
        """Support user should be able to access resource history."""
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_cannot_access_history(self):
        """Customer owner should not be able to access resource history."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_cannot_access_history(self):
        """Project admin should not be able to access resource history."""
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_user_cannot_access_history(self):
        """Unauthenticated user should not be able to access resource history."""
        response = self.client.get(self._get_history_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_history_at_returns_correct_version_for_timestamp(self):
        """history/at endpoint should return correct version for given timestamp."""
        self.client.force_authenticate(self.fixture.staff)

        # Create initial version
        with reversion.create_revision():
            self.resource.name = "Name v1"
            self.resource.save()
            reversion.set_user(self.fixture.staff)
            reversion.set_comment("First version")

        first_version_time = timezone.now()

        # Wait a moment and create second version
        with reversion.create_revision():
            self.resource.name = "Name v2"
            self.resource.save()
            reversion.set_user(self.fixture.staff)
            reversion.set_comment("Second version")

        # Query for state at first version time
        url = self._get_history_at_url()
        response = self.client.get(url, {"timestamp": first_version_time.isoformat()})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["serialized_data"]["name"], "Name v1")

    def test_history_at_returns_404_for_timestamp_before_any_version(self):
        """history/at should return 404 if no version exists before timestamp."""
        self.client.force_authenticate(self.fixture.staff)

        # Query for state before any versions exist
        past_time = timezone.now() - timedelta(days=365)
        url = self._get_history_at_url()
        response = self.client.get(url, {"timestamp": past_time.isoformat()})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_history_at_requires_timestamp_parameter(self):
        """history/at should require timestamp parameter."""
        self.client.force_authenticate(self.fixture.staff)
        url = self._get_history_at_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_history_at_validates_timestamp_format(self):
        """history/at should validate timestamp format."""
        self.client.force_authenticate(self.fixture.staff)
        url = self._get_history_at_url()
        response = self.client.get(url, {"timestamp": "invalid-date"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ResourceHistoryFilteringTest(test.APITransactionTestCase):
    """Test resource history filtering."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

    def _get_history_url(self):
        return factories.ResourceFactory.get_url(self.resource, "history")

    def _create_versions_with_timestamps(self):
        """Create versions with known timestamps for testing."""
        self.time_before_all = timezone.now()

        # First version
        with reversion.create_revision():
            self.resource.name = "Name v1"
            self.resource.save()
            reversion.set_user(self.fixture.staff)

        self.time_after_first = timezone.now()

        # Second version
        with reversion.create_revision():
            self.resource.name = "Name v2"
            self.resource.save()
            reversion.set_user(self.fixture.staff)

        self.time_after_second = timezone.now()

        # Third version
        with reversion.create_revision():
            self.resource.name = "Name v3"
            self.resource.save()
            reversion.set_user(self.fixture.staff)

        self.time_after_all = timezone.now()

    def test_filter_by_created_before(self):
        """Filter versions created before a timestamp."""
        self._create_versions_with_timestamps()
        self.client.force_authenticate(self.fixture.staff)

        url = self._get_history_url()
        response = self.client.get(
            url, {"created_before": self.time_after_second.isoformat()}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return initial + first two versions (created before time_after_second)
        self.assertEqual(len(response.data), 3)

    def test_filter_by_created_after(self):
        """Filter versions created after a timestamp."""
        self._create_versions_with_timestamps()
        self.client.force_authenticate(self.fixture.staff)

        url = self._get_history_url()
        response = self.client.get(
            url, {"created_after": self.time_after_first.isoformat()}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return last two versions (created after time_after_first)
        self.assertEqual(len(response.data), 2)

    def test_filter_with_both_created_before_and_after(self):
        """Filter versions with both before and after timestamps."""
        self._create_versions_with_timestamps()
        self.client.force_authenticate(self.fixture.staff)

        url = self._get_history_url()
        response = self.client.get(
            url,
            {
                "created_after": self.time_after_first.isoformat(),
                "created_before": self.time_after_all.isoformat(),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return second and third versions
        self.assertEqual(len(response.data), 2)


class ResourceActionReversionTest(test.APITransactionTestCase):
    """Test that resource actions create reversion entries."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

    def _get_action_url(self, action):
        return factories.ResourceFactory.get_url(self.resource, action)

    def test_set_slug_creates_revision(self):
        """set_slug action should create a revision."""
        self.client.force_authenticate(self.fixture.staff)

        # Get initial version count
        initial_count = Version.objects.get_for_object(self.resource).count()

        url = self._get_action_url("set_slug")
        self.client.post(url, {"slug": "new-slug"})

        # Verify a new version was created
        new_count = Version.objects.get_for_object(self.resource).count()
        self.assertEqual(new_count, initial_count + 1)

        # Verify the version has correct comment
        latest_version = Version.objects.get_for_object(self.resource).first()
        self.assertIn("Slug changed", latest_version.revision.comment)

    def test_set_downscaled_creates_revision(self):
        """set_downscaled action should create a revision."""
        self.client.force_authenticate(self.fixture.staff)

        # Get initial version count
        initial_count = Version.objects.get_for_object(self.resource).count()

        url = self._get_action_url("set_downscaled")
        self.client.post(url, {"downscaled": True})

        # Verify a new version was created
        new_count = Version.objects.get_for_object(self.resource).count()
        self.assertEqual(new_count, initial_count + 1)

        # Verify the version has correct comment
        latest_version = Version.objects.get_for_object(self.resource).first()
        self.assertIn("Downscaled changed", latest_version.revision.comment)

    def test_set_paused_creates_revision(self):
        """set_paused action should create a revision."""
        self.client.force_authenticate(self.fixture.staff)

        # Get initial version count
        initial_count = Version.objects.get_for_object(self.resource).count()

        url = self._get_action_url("set_paused")
        self.client.post(url, {"paused": True})

        # Verify a new version was created
        new_count = Version.objects.get_for_object(self.resource).count()
        self.assertEqual(new_count, initial_count + 1)

        # Verify the version has correct comment
        latest_version = Version.objects.get_for_object(self.resource).first()
        self.assertIn("Paused changed", latest_version.revision.comment)

    def test_set_restrict_member_access_creates_revision(self):
        """set_restrict_member_access action should create a revision."""
        self.client.force_authenticate(self.fixture.staff)

        # Get initial version count
        initial_count = Version.objects.get_for_object(self.resource).count()

        url = self._get_action_url("set_restrict_member_access")
        self.client.post(url, {"restrict_member_access": True})

        # Verify a new version was created
        new_count = Version.objects.get_for_object(self.resource).count()
        self.assertEqual(new_count, initial_count + 1)

        # Verify the version has correct comment
        latest_version = Version.objects.get_for_object(self.resource).first()
        self.assertIn("Restrict member access changed", latest_version.revision.comment)

    def test_resource_update_creates_revision(self):
        """Standard resource update should create a revision."""
        self.client.force_authenticate(self.fixture.staff)

        # Get initial version count
        initial_count = Version.objects.get_for_object(self.resource).count()

        url = factories.ResourceFactory.get_url(self.resource)
        self.client.patch(url, {"name": "Updated Name"})

        # Verify a new version was created
        new_count = Version.objects.get_for_object(self.resource).count()
        self.assertEqual(new_count, initial_count + 1)

    def test_revision_tracks_user(self):
        """Revision should track the user who made the change."""
        self.client.force_authenticate(self.fixture.staff)

        url = self._get_action_url("set_slug")
        self.client.post(url, {"slug": "new-slug"})

        latest_version = Version.objects.get_for_object(self.resource).first()
        self.assertEqual(latest_version.revision.user, self.fixture.staff)


class CreateReversionMixinBugTest(unittest.TestCase):
    """Test that CreateReversionMixin correctly delegates to perform_create.

    Bug: CreateReversionMixin.perform_create() calls super().perform_update()
    instead of super().perform_create(), so create operations do not produce
    an initial revision.
    """

    def test_create_reversion_mixin_calls_perform_create_not_perform_update(self):
        """CreateReversionMixin.perform_create should delegate to super().perform_create."""
        import inspect

        source = inspect.getsource(CreateReversionMixin.perform_create)
        self.assertIn(
            "super().perform_create(serializer)",
            source,
            "CreateReversionMixin.perform_create() must call super().perform_create(), "
            "not super().perform_update(). This is a bug that prevents initial revision "
            "creation on object creation.",
        )


class OfferingCreationRevisionTest(test.APITransactionTestCase):
    """Test that creating an offering via the API records an initial revision.

    Currently fails because:
    1. CreateReversionMixin has a bug (calls perform_update instead of perform_create)
    2. Even if fixed, the offering creation flow may not go through perform_create
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_offering_has_initial_revision_after_creation(self):
        """Creating an offering should record an initial version for history tracking."""
        offering = self.fixture.offering
        versions = Version.objects.get_for_object(offering)
        self.assertGreaterEqual(
            versions.count(),
            1,
            "Offering should have at least one version after creation, "
            "so that the initial state is available in the history API. "
            "Currently no initial revision is recorded.",
        )


class ResourceCreationRevisionTest(test.APITransactionTestCase):
    """Test that resources have an initial revision after creation.

    The history API returns empty for newly created resources because
    no initial revision is recorded. This means the creation state is lost.
    """

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

    def test_resource_has_initial_revision_after_creation(self):
        """A newly created resource should have at least one version in history."""
        versions = Version.objects.get_for_object(self.resource)
        self.assertGreaterEqual(
            versions.count(),
            1,
            "Resource should have at least one version after creation, "
            "so that the initial state is available in the history API. "
            "Currently no initial revision is recorded.",
        )

    def test_resource_history_endpoint_returns_initial_state(self):
        """History endpoint should return the initial state of a new resource."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ResourceFactory.get_url(self.resource, "history")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(
            len(response.data),
            1,
            "History endpoint should return at least one entry for a new resource, "
            "representing its initial state at creation time.",
        )


class PlanCreationRevisionTest(test.APITransactionTestCase):
    """Test that plans have an initial revision after creation."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()

    def test_plan_has_initial_revision_after_creation(self):
        """A newly created plan should have at least one version in history."""
        plan = self.fixture.plan
        versions = Version.objects.get_for_object(plan)
        self.assertGreaterEqual(
            versions.count(),
            1,
            "Plan should have at least one version after creation, "
            "so that the initial state is available in the history API. "
            "Currently no initial revision is recorded.",
        )
