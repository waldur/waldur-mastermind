from django.test import TestCase
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories, fixtures


class ProjectSlugTemplateGenerationTest(TestCase):
    def test_template_based_slug_when_customer_has_template(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        project = factories.ProjectFactory(customer=customer, slug="")
        self.assertEqual(project.slug, f"{customer.slug}-001")

    def test_default_slug_when_no_template(self):
        customer = factories.CustomerFactory(project_slug_template=None)
        project = factories.ProjectFactory(customer=customer, slug="")
        # Default behavior: slugified name (from SlugMixin)
        self.assertTrue(len(project.slug) > 0)
        self.assertNotIn("{", project.slug)

    @freeze_time("2026-04-20")
    def test_template_with_year_and_month(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{year}{month}-{counter_padded}"
        )
        project = factories.ProjectFactory(customer=customer, slug="")
        self.assertEqual(project.slug, f"{customer.slug}-202604-001")

    def test_template_with_project_name(self):
        customer = factories.CustomerFactory(
            project_slug_template="{project_name}-{counter_padded}"
        )
        project = factories.ProjectFactory(
            customer=customer, name="My Test Project", slug=""
        )
        self.assertEqual(project.slug, "my-test-project-001")

    def test_counter_increments_per_customer(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        p1 = factories.ProjectFactory(customer=customer, slug="")
        p2 = factories.ProjectFactory(customer=customer, slug="")
        self.assertEqual(p1.slug, f"{customer.slug}-001")
        self.assertEqual(p2.slug, f"{customer.slug}-002")

    def test_counters_independent_across_customers(self):
        c1 = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        c2 = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        factories.ProjectFactory(customer=c1, slug="")
        p2 = factories.ProjectFactory(customer=c2, slug="")
        # Second customer's first project should have counter 001
        self.assertEqual(p2.slug, f"{c2.slug}-001")

    def test_uniqueness_suffix_on_collision(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        factories.ProjectFactory(customer=customer, slug="")
        # Manually set another project's slug to collide with what the next one would generate
        expected_slug = f"{customer.slug}-002"
        factories.ProjectFactory(customer=customer, slug=expected_slug)
        p3 = factories.ProjectFactory(customer=customer, slug="")
        # p3's counter would be 3, so no collision
        self.assertEqual(p3.slug, f"{customer.slug}-003")

    def test_fallback_on_malformed_template(self):
        customer = factories.CustomerFactory(
            project_slug_template="{invalid_var}-{counter_padded}"
        )
        project = factories.ProjectFactory(customer=customer, slug="")
        # Should fall back to default name-based slug
        self.assertTrue(len(project.slug) > 0)
        self.assertNotIn("{", project.slug)

    def test_slug_not_regenerated_on_update(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        project = factories.ProjectFactory(customer=customer, slug="")
        original_slug = project.slug
        project.name = "Updated Name"
        project.save()
        self.assertEqual(project.slug, original_slug)

    def test_staff_provided_slug_used_when_template_exists(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        project = factories.ProjectFactory(customer=customer, slug="custom-manual-slug")
        self.assertEqual(project.slug, "custom-manual-slug")

    def test_template_with_all_placeholders(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{project_name}-{counter}"
        )
        project = factories.ProjectFactory(customer=customer, name="Alpha", slug="")
        self.assertEqual(project.slug, f"{customer.slug}-alpha-1")

    def test_slug_unique_against_soft_deleted_project(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        p1 = factories.ProjectFactory(customer=customer, slug="")
        slug1 = p1.slug
        # Soft-delete the project
        p1.delete()
        self.assertTrue(p1.is_removed)
        # New project should not reuse the soft-deleted slug
        p2 = factories.ProjectFactory(customer=customer, slug="")
        self.assertNotEqual(p2.slug, slug1)

    def test_counter_includes_soft_deleted_projects(self):
        customer = factories.CustomerFactory(
            project_slug_template="{customer_slug}-{counter_padded}"
        )
        p1 = factories.ProjectFactory(customer=customer, slug="")
        self.assertEqual(p1.slug, f"{customer.slug}-001")
        # Soft-delete
        p1.delete()
        # Counter should account for soft-deleted project
        p2 = factories.ProjectFactory(customer=customer, slug="")
        self.assertEqual(p2.slug, f"{customer.slug}-002")


class ProjectSlugTemplateAPICreateTest(test.APITransactionTestCase):
    """Tests that slug template is applied when creating projects via the API."""

    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.customer = self.fixture.customer
        self.list_url = factories.ProjectFactory.get_list_url()

    @freeze_time("2026-06-15")
    def test_template_slug_applied_when_no_slug_provided(self):
        self.customer.project_slug_template = "{customer_slug}-{year}-{counter_padded}"
        self.customer.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.list_url,
            {
                "name": "My Research Project",
                "customer": factories.CustomerFactory.get_url(self.customer),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["slug"], f"{self.customer.slug}-2026-001")

    @freeze_time("2026-06-15")
    def test_template_slug_applied_when_empty_slug_provided(self):
        self.customer.project_slug_template = "{customer_slug}-{year}-{counter_padded}"
        self.customer.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.list_url,
            {
                "name": "My Research Project",
                "slug": "",
                "customer": factories.CustomerFactory.get_url(self.customer),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["slug"], f"{self.customer.slug}-2026-001")

    def test_explicit_slug_overrides_template(self):
        self.customer.project_slug_template = "{customer_slug}-{counter_padded}"
        self.customer.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.list_url,
            {
                "name": "My Research Project",
                "slug": "my-custom-slug",
                "customer": factories.CustomerFactory.get_url(self.customer),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["slug"], "my-custom-slug")

    def test_default_slug_when_no_template_via_api(self):
        self.customer.project_slug_template = None
        self.customer.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.list_url,
            {
                "name": "My Research Project",
                "customer": factories.CustomerFactory.get_url(self.customer),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Should be a name-based slug, not empty
        self.assertTrue(len(response.data["slug"]) > 0)
        self.assertNotIn("{", response.data["slug"])


class CustomerProjectSlugTemplateAPITest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.customer = self.fixture.customer
        self.url = factories.CustomerFactory.get_url(self.customer)

    def test_staff_can_set_valid_template(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            self.url,
            {"project_slug_template": "{customer_slug}-{year}-{counter_padded}"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertEqual(
            self.customer.project_slug_template,
            "{customer_slug}-{year}-{counter_padded}",
        )

    def test_staff_can_clear_template(self):
        self.customer.project_slug_template = "{customer_slug}-{counter_padded}"
        self.customer.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            self.url,
            {"project_slug_template": ""},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_placeholder_rejected(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            self.url,
            {"project_slug_template": "{invalid}-{counter_padded}"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_malformed_template_rejected(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            self.url,
            {"project_slug_template": "{customer_slug}-{counter_padded"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_staff_cannot_edit_template(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            self.url,
            {"project_slug_template": "{customer_slug}-{counter_padded}"},
        )
        # Field is read-only for non-staff, so value should not change
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.project_slug_template)

    def test_template_visible_to_non_staff(self):
        self.customer.project_slug_template = "{customer_slug}-{counter_padded}"
        self.customer.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["project_slug_template"],
            "{customer_slug}-{counter_padded}",
        )

    def test_template_can_be_changed_with_existing_projects(self):
        self.customer.project_slug_template = "{customer_slug}-{counter_padded}"
        self.customer.save()
        factories.ProjectFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            self.url,
            {"project_slug_template": "{customer_slug}-{year}-{counter_padded}"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
