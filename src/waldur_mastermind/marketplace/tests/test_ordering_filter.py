"""Tests for ordering functionality in OfferingUserChecklistCompletionsFilter."""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class OrderingFilterTest(test.APITransactionTestCase):
    """Test ordering functionality for checklist completions."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.user = self.fixture.user

        # Create offerings with checklists
        self.offering1 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="A First Offering",
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        self.checklist1 = checklist_factories.ChecklistFactory(name="Checklist 1")
        self.offering1.compliance_checklist = self.checklist1
        self.offering1.save()

        self.offering2 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Z Last Offering",
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        self.checklist2 = checklist_factories.ChecklistFactory(name="Checklist 2")
        self.offering2.compliance_checklist = self.checklist2
        self.offering2.save()

        # Create offering users
        self.offering_user1 = factories.OfferingUserFactory(
            offering=self.offering1, user=self.user, username="user1"
        )
        self.offering_user2 = factories.OfferingUserFactory(
            offering=self.offering2, user=self.user, username="user2"
        )

        # Create completions with different statuses and times
        content_type = ContentType.objects.get_for_model(models.OfferingUser)

        # First completion - not completed
        self.completion1, _ = (
            checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=self.checklist1,
                scope_content_type=content_type,
                scope_object_id=self.offering_user1.id,
                defaults={"is_completed": False},
            )
        )
        self.completion1.is_completed = False
        self.completion1.save()

        # Second completion - completed
        self.completion2, _ = (
            checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=self.checklist2,
                scope_content_type=content_type,
                scope_object_id=self.offering_user2.id,
                defaults={"is_completed": True},
            )
        )
        self.completion2.is_completed = True
        self.completion2.save()

        # Update modification times to ensure ordering
        import datetime

        from django.utils import timezone

        base_time = timezone.now()

        # Make completion1 older
        self.completion1.modified = base_time - datetime.timedelta(hours=1)
        self.completion1.save()

        # Make completion2 newer
        self.completion2.modified = base_time
        self.completion2.save()

        self.url = "/api/marketplace-offering-user-checklist-completions/"

    def test_default_ordering_by_modified_desc(self):
        """Test default ordering is by modified timestamp descending."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # Newer completion (completion2) should come first
        first_result = response.data[0]
        second_result = response.data[1]

        # Verify ordering by checking which offering name appears first
        # Z Last Offering (completion2) should come before A First Offering (completion1)
        self.assertEqual(first_result["offering_name"], "Z Last Offering")
        self.assertEqual(second_result["offering_name"], "A First Offering")

    def test_ordering_by_modified_asc(self):
        """Test explicit ordering by modified timestamp ascending."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"o": "modified"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # Older completion (completion1) should come first
        first_result = response.data[0]
        second_result = response.data[1]

        self.assertEqual(first_result["offering_name"], "A First Offering")
        self.assertEqual(second_result["offering_name"], "Z Last Offering")

    def test_ordering_by_is_completed_asc(self):
        """Test ordering by completion status ascending (False before True)."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"o": "is_completed"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # Not completed (False) should come before completed (True)
        first_result = response.data[0]
        second_result = response.data[1]

        self.assertFalse(first_result["is_completed"])
        self.assertTrue(second_result["is_completed"])

    def test_ordering_by_is_completed_desc(self):
        """Test ordering by completion status descending (True before False)."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"o": "-is_completed"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # Completed (True) should come before not completed (False)
        first_result = response.data[0]
        second_result = response.data[1]

        self.assertTrue(first_result["is_completed"])
        self.assertFalse(second_result["is_completed"])

    def test_invalid_ordering_field(self):
        """Test that invalid ordering fields return 400 error."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"o": "invalid_field"})

        # Should return bad request for invalid ordering field
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_multiple_ordering_fields(self):
        """Test ordering by multiple fields."""
        # Create a third completion with same completion status as first
        offering3 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="B Middle Offering",
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        checklist3 = checklist_factories.ChecklistFactory(name="Checklist 3")
        offering3.compliance_checklist = checklist3
        offering3.save()

        offering_user3 = factories.OfferingUserFactory(
            offering=offering3, user=self.user, username="user3"
        )

        content_type = ContentType.objects.get_for_model(models.OfferingUser)
        completion3, _ = checklist_models.ChecklistCompletion.objects.get_or_create(
            checklist=checklist3,
            scope_content_type=content_type,
            scope_object_id=offering_user3.id,
            defaults={"is_completed": False},  # Same as completion1
        )
        completion3.is_completed = False
        completion3.save()

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"o": "is_completed,modified"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

        # Should order by is_completed first, then by modified
        # All False completions should come before True completions


class FilterOrderingIntegrationTest(test.APITransactionTestCase):
    """Test integration between filtering and ordering."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.user = self.fixture.user

        # Create multiple offerings and completions for integration testing
        self.offerings = []
        self.completions = []

        for i in range(3):
            offering = factories.OfferingFactory(
                customer=self.fixture.customer,
                name=f"Offering {i}",
                plugin_options={"service_provider_can_create_offering_user": True},
            )
            checklist = checklist_factories.ChecklistFactory(name=f"Checklist {i}")
            offering.compliance_checklist = checklist
            offering.save()

            offering_user = factories.OfferingUserFactory(
                offering=offering, user=self.user, username=f"user{i}"
            )

            content_type = ContentType.objects.get_for_model(models.OfferingUser)
            completion, _ = checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id=offering_user.id,
                defaults={"is_completed": (i % 2 == 0)},  # Alternate completion status
            )
            completion.is_completed = i % 2 == 0
            completion.save()

            self.offerings.append(offering)
            self.completions.append(completion)

        self.url = "/api/marketplace-offering-user-checklist-completions/"

    def test_filter_by_completion_status_with_ordering(self):
        """Test filtering by completion status combined with ordering."""
        self.client.force_authenticate(self.user)

        # Filter for completed items and order by modified
        response = self.client.get(self.url, {"is_completed": "true", "o": "modified"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # All results should be completed
        for result in response.data:
            self.assertTrue(result["is_completed"])

    def test_filter_by_offering_with_ordering(self):
        """Test filtering by offering UUID combined with ordering."""
        self.client.force_authenticate(self.user)

        # Filter for specific offering and order by completion status
        response = self.client.get(
            self.url,
            {"offering_uuid": str(self.offerings[0].uuid), "o": "-is_completed"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only return completions for the specified offering
        for result in response.data:
            self.assertEqual(result["offering_uuid"], str(self.offerings[0].uuid))

    def test_filter_by_user_with_ordering(self):
        """Test filtering by user UUID combined with ordering."""
        self.client.force_authenticate(self.user)

        # Filter for current user and order by modified descending
        response = self.client.get(
            self.url, {"user_uuid": str(self.user.uuid), "o": "-modified"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)  # All completions belong to this user

        # Verify ordering - newer items should come first
        if len(response.data) >= 2:
            first_modified = response.data[0]["modified"]
            second_modified = response.data[1]["modified"]
            # First should be newer than or equal to second
            self.assertGreaterEqual(first_modified, second_modified)
