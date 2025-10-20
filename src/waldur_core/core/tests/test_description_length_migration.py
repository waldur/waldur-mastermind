"""
Tests for description field length migration validation.

This module validates that all models with description fields can store
exactly 4096 characters and properly reject strings longer than the limit.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from waldur_core.core.models import DESCRIPTION_LENGTH
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support.tests import factories as support_factories


class DescriptionLengthMigrationTest(TestCase):
    """Test description field length limits after migration to 4096 characters."""

    def test_description_length_constant_updated(self):
        """Test that DESCRIPTION_LENGTH constant is set to 4096."""
        self.assertEqual(DESCRIPTION_LENGTH, 4096)

    def test_customer_description_max_length(self):
        """Test Customer model description field length validation."""
        # Test exactly at limit - should succeed
        valid_description = "A" * DESCRIPTION_LENGTH
        customer = structure_factories.CustomerFactory(description=valid_description)
        customer.full_clean()  # Should not raise

        # Test exceeding limit - should fail
        invalid_description = "A" * (DESCRIPTION_LENGTH + 1)
        customer = structure_factories.CustomerFactory.build(
            description=invalid_description
        )

        with self.assertRaises(ValidationError) as context:
            customer.full_clean()
        self.assertIn("description", str(context.exception))

    def test_project_description_max_length(self):
        """Test Project model description field length validation."""
        # Test exactly at limit - should succeed
        valid_description = "B" * DESCRIPTION_LENGTH
        project = structure_factories.ProjectFactory(description=valid_description)
        project.full_clean()  # Should not raise

        # Test exceeding limit - should fail
        invalid_description = "B" * (DESCRIPTION_LENGTH + 1)
        project = structure_factories.ProjectFactory.build(
            description=invalid_description
        )

        with self.assertRaises(ValidationError) as context:
            project.full_clean()
        self.assertIn("description", str(context.exception))

    def test_priority_description_max_length(self):
        """Test Priority model description field length validation."""
        # Test exactly at limit - should succeed
        valid_description = "E" * DESCRIPTION_LENGTH
        priority = support_factories.PriorityFactory(description=valid_description)
        priority.full_clean()  # Should not raise

        # Test exceeding limit - should fail
        invalid_description = "E" * (DESCRIPTION_LENGTH + 1)
        priority = support_factories.PriorityFactory.build(
            description=invalid_description
        )

        with self.assertRaises(ValidationError) as context:
            priority.full_clean()
        self.assertIn("description", str(context.exception))

    def test_description_field_can_store_common_lengths(self):
        """Test that description fields can store common content lengths."""
        # Test empty description
        customer = structure_factories.CustomerFactory(description="")
        customer.full_clean()

        # Test moderate length description (500 chars)
        moderate_description = (
            "This is a moderate length description. " * 12
        )  # ~480 chars
        customer = structure_factories.CustomerFactory(description=moderate_description)
        customer.full_clean()

        # Test long description (2000 chars - old limit)
        long_description = (
            "This is a long description that would have hit the old 2000 character limit. "
            * 25
        )  # ~1950 chars
        customer = structure_factories.CustomerFactory(description=long_description)
        customer.full_clean()

        # Test very long description (near new limit)
        very_long_description = "X" * (DESCRIPTION_LENGTH - 10)  # 4086 chars
        customer = structure_factories.CustomerFactory(
            description=very_long_description
        )
        customer.full_clean()

    def test_description_persistence(self):
        """Test that description content is properly stored and retrieved."""
        test_content = "Test description content: " + "A" * 100

        # Create and save
        customer = structure_factories.CustomerFactory(description=test_content)
        customer.save()

        # Retrieve and verify
        customer.refresh_from_db()
        self.assertEqual(customer.description, test_content)

        # Test with exactly maximum length
        max_content = "Z" * DESCRIPTION_LENGTH
        customer.description = max_content
        customer.save()

        customer.refresh_from_db()
        self.assertEqual(customer.description, max_content)
        self.assertEqual(len(customer.description), DESCRIPTION_LENGTH)

    def test_edge_case_lengths(self):
        """Test edge cases around the length limit."""
        # Test exactly at limit
        exact_limit_desc = "M" * DESCRIPTION_LENGTH
        customer = structure_factories.CustomerFactory(description=exact_limit_desc)
        customer.full_clean()

        # Test one character over limit
        over_limit_desc = "N" * (DESCRIPTION_LENGTH + 1)
        customer = structure_factories.CustomerFactory.build(
            description=over_limit_desc
        )

        with self.assertRaises(ValidationError):
            customer.full_clean()

        # Test significantly over limit
        way_over_limit_desc = "O" * (DESCRIPTION_LENGTH + 1000)
        customer = structure_factories.CustomerFactory.build(
            description=way_over_limit_desc
        )

        with self.assertRaises(ValidationError):
            customer.full_clean()
