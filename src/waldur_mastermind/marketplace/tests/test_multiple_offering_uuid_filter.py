from django.test import TestCase

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.filters import ScreenshotFilter
from waldur_mastermind.marketplace.tests import factories


class MultipleOfferingUUIDFilterTest(TestCase):
    def setUp(self):
        # Create test offerings
        self.offering1 = factories.OfferingFactory()
        self.offering2 = factories.OfferingFactory()
        self.offering3 = factories.OfferingFactory()

        # Create screenshots for each offering
        self.screenshot1 = factories.ScreenshotFactory(offering=self.offering1)
        self.screenshot2 = factories.ScreenshotFactory(offering=self.offering2)
        self.screenshot3 = factories.ScreenshotFactory(offering=self.offering3)

    def test_filter_by_single_offering_uuid(self):
        """Test filtering by a single offering_uuid."""
        filterset = ScreenshotFilter(
            data={"offering_uuid": str(self.offering1.uuid)},
            queryset=models.Screenshot.objects.all(),
        )

        filtered_qs = filterset.qs
        self.assertEqual(filtered_qs.count(), 1)
        self.assertIn(self.screenshot1, filtered_qs)
        self.assertNotIn(self.screenshot2, filtered_qs)
        self.assertNotIn(self.screenshot3, filtered_qs)

    def test_filter_by_multiple_offering_uuids_as_comma_separated(self):
        """Test filtering by multiple offering_uuids passed as comma-separated string."""
        filterset = ScreenshotFilter(
            data={"offering_uuid": f"{self.offering1.uuid},{self.offering3.uuid}"},
            queryset=models.Screenshot.objects.all(),
        )

        filtered_qs = filterset.qs
        self.assertEqual(filtered_qs.count(), 2)
        self.assertIn(self.screenshot1, filtered_qs)
        self.assertNotIn(self.screenshot2, filtered_qs)
        self.assertIn(self.screenshot3, filtered_qs)

    def test_filter_returns_empty_when_no_match(self):
        """Test that filter returns empty queryset when no offerings match."""
        import uuid

        non_existent_uuid = str(uuid.uuid4())

        filterset = ScreenshotFilter(
            data={"offering_uuid": non_existent_uuid},
            queryset=models.Screenshot.objects.all(),
        )

        filtered_qs = filterset.qs
        self.assertEqual(filtered_qs.count(), 0)


class MultipleOfferingSlugFilterTest(TestCase):
    def setUp(self):
        # Create test offerings with specific slugs
        self.offering1 = factories.OfferingFactory(name="First Offering")
        self.offering2 = factories.OfferingFactory(name="Second Offering")
        self.offering3 = factories.OfferingFactory(name="Third Offering")

        # Create screenshots for each offering
        self.screenshot1 = factories.ScreenshotFactory(offering=self.offering1)
        self.screenshot2 = factories.ScreenshotFactory(offering=self.offering2)
        self.screenshot3 = factories.ScreenshotFactory(offering=self.offering3)

    def test_filter_by_single_offering_slug(self):
        """Test filtering by a single offering_slug."""
        filterset = ScreenshotFilter(
            data={"offering_slug": self.offering1.slug},
            queryset=models.Screenshot.objects.all(),
        )

        filtered_qs = filterset.qs
        self.assertEqual(filtered_qs.count(), 1)
        self.assertIn(self.screenshot1, filtered_qs)
        self.assertNotIn(self.screenshot2, filtered_qs)
        self.assertNotIn(self.screenshot3, filtered_qs)

    def test_filter_by_multiple_offering_slugs_as_comma_separated(self):
        """Test filtering by multiple offering_slugs passed as comma-separated string."""
        filterset = ScreenshotFilter(
            data={"offering_slug": f"{self.offering1.slug},{self.offering3.slug}"},
            queryset=models.Screenshot.objects.all(),
        )

        filtered_qs = filterset.qs
        self.assertEqual(filtered_qs.count(), 2)
        self.assertIn(self.screenshot1, filtered_qs)
        self.assertNotIn(self.screenshot2, filtered_qs)
        self.assertIn(self.screenshot3, filtered_qs)

    def test_filter_returns_empty_when_slug_no_match(self):
        """Test that filter returns empty queryset when no offerings match."""
        filterset = ScreenshotFilter(
            data={"offering_slug": "non-existent-slug"},
            queryset=models.Screenshot.objects.all(),
        )

        filtered_qs = filterset.qs
        self.assertEqual(filtered_qs.count(), 0)

    def test_filter_by_combined_uuid_and_slug_filters(self):
        """Test that UUID and slug filters work together as AND conditions."""
        # Filter by offering1's UUID and offering1's slug - should return only offering1
        filterset = ScreenshotFilter(
            data={
                "offering_uuid": str(self.offering1.uuid),
                "offering_slug": self.offering1.slug,
            },
            queryset=models.Screenshot.objects.all(),
        )

        filtered_qs = filterset.qs
        self.assertEqual(filtered_qs.count(), 1)
        self.assertIn(self.screenshot1, filtered_qs)
        self.assertNotIn(self.screenshot2, filtered_qs)
        self.assertNotIn(self.screenshot3, filtered_qs)

    def test_filter_by_mismatched_uuid_and_slug_returns_empty(self):
        """Test that mismatched UUID and slug filters return empty result."""
        # Filter by offering1's UUID and offering2's slug - should return nothing
        filterset = ScreenshotFilter(
            data={
                "offering_uuid": str(self.offering1.uuid),
                "offering_slug": self.offering2.slug,
            },
            queryset=models.Screenshot.objects.all(),
        )

        filtered_qs = filterset.qs
        self.assertEqual(filtered_qs.count(), 0)
