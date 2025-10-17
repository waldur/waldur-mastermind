from django.conf import settings
from django.db import models
from django.test import TestCase
from django.utils import timezone

from waldur_core.core.models import User, generate_slug


class TestModels(TestCase):
    def test_token_lifetime_is_read_from_settings_as_default_value_when_user_is_created(
        self,
    ):
        waldur_core_settings = settings.WALDUR_CORE.copy()
        waldur_core_settings["TOKEN_LIFETIME"] = timezone.timedelta(days=1)

        with self.settings(WALDUR_CORE=waldur_core_settings):
            token_lifetime = settings.WALDUR_CORE["TOKEN_LIFETIME"]
            expected_lifetime = int(token_lifetime.total_seconds())
            user = User.objects.create(username="test1")
            self.assertEqual(user.token_lifetime, expected_lifetime)

    def test_when_user_is_created_query_field_is_filled(self):
        user = User.objects.create_user(username="jb007", full_name="J̋̀a̻͢m̪̄e̪͊s̯̊ B̝͆on͎̂d")
        self.assertEqual(user.query_field, "James Bond")


class TestSlugModel(models.Model):
    """Test model for slug generation testing"""

    slug = models.SlugField(max_length=255, unique=True)

    class Meta:
        app_label = "core"


class GenerateSlugTest(TestCase):
    def setUp(self):
        # Clean up any existing test data
        TestSlugModel.objects.all().delete()

    def test_generate_slug_returns_base_slug_when_no_conflicts(self):
        """Should return the base slug when no existing slugs conflict"""
        slug = generate_slug("test name", TestSlugModel)
        self.assertEqual(slug, "test-name")

    def test_generate_slug_returns_base_slug_when_no_existing_slugs(self):
        """Should return the base slug when no slugs exist at all"""
        slug = generate_slug("unique", TestSlugModel)
        self.assertEqual(slug, "unique")

    def test_generate_slug_returns_numbered_slug_when_base_exists(self):
        """Should return slug-2 when base slug already exists"""
        # Create an existing slug
        TestSlugModel.objects.create(slug="project")

        slug = generate_slug("project", TestSlugModel)
        self.assertEqual(slug, "project-2")

    def test_generate_slug_increments_correctly_with_multiple_conflicts(self):
        """Should find the highest number and increment by 1"""
        # Create existing slugs
        TestSlugModel.objects.create(slug="test")
        TestSlugModel.objects.create(slug="test-2")
        TestSlugModel.objects.create(slug="test-3")
        TestSlugModel.objects.create(slug="test-5")  # Gap in sequence

        slug = generate_slug("test", TestSlugModel)
        self.assertEqual(slug, "test-6")  # Should be highest + 1

    def test_generate_slug_handles_non_numeric_suffixes(self):
        """Should ignore slugs with non-numeric suffixes"""
        TestSlugModel.objects.create(slug="project")
        TestSlugModel.objects.create(slug="project-abc")
        TestSlugModel.objects.create(slug="project-xyz")
        TestSlugModel.objects.create(slug="project-2")

        slug = generate_slug("project", TestSlugModel)
        self.assertEqual(slug, "project-3")

    def test_generate_slug_with_complex_base_slug(self):
        """Should work with complex base slugs containing hyphens"""
        # Note: SLUG_NAME_LIMIT is 10, so "my-complex" is the actual base slug
        TestSlugModel.objects.create(slug="my-complex")
        TestSlugModel.objects.create(slug="my-complex-2")

        slug = generate_slug("My Complex Project", TestSlugModel)
        self.assertEqual(slug, "my-complex-3")

    def test_generate_slug_sequence_pattern(self):
        """Should follow the pattern: slug, slug-2, slug-3, ..."""
        name = "example"

        # First call: should return "example"
        slug1 = generate_slug(name, TestSlugModel)
        self.assertEqual(slug1, "example")
        TestSlugModel.objects.create(slug=slug1)

        # Second call: should return "example-2"
        slug2 = generate_slug(name, TestSlugModel)
        self.assertEqual(slug2, "example-2")
        TestSlugModel.objects.create(slug=slug2)

        # Third call: should return "example-3"
        slug3 = generate_slug(name, TestSlugModel)
        self.assertEqual(slug3, "example-3")

    def test_generate_slug_with_empty_name(self):
        """Should handle empty or whitespace-only names"""
        slug = generate_slug("", TestSlugModel)
        self.assertEqual(slug, "")

        slug = generate_slug("   ", TestSlugModel)
        self.assertEqual(slug, "")

    def test_generate_slug_with_special_characters(self):
        """Should properly slugify names with special characters"""
        # Note: SLUG_NAME_LIMIT is 10, so "test-speci" is the actual base slug
        slug = generate_slug("Test & Special Characters!", TestSlugModel)
        self.assertEqual(slug, "test-speci")

    def test_generate_slug_case_insensitive_matching(self):
        """Should work correctly regardless of case in input"""
        # Note: SLUG_NAME_LIMIT is 10, so "test-proje" is the actual base slug
        TestSlugModel.objects.create(slug="test-proje")

        slug = generate_slug("Test Project", TestSlugModel)
        self.assertEqual(slug, "test-proje-2")
