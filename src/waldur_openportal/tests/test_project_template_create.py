"""Who may create a project template, and for which organizations (#1063).

Creating a template requires owning its provider organization (or staff). The
organization the template's projects are placed in must be one the caller's
roles reach, as when the template is updated. Global support only reads, so
on these writes it reaches what its roles do, like anyone else.
"""

from django.urls import reverse
from rest_framework import test

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_openportal import models


class ProjectTemplateCreateTest(test.APITestCase):
    def setUp(self):
        self.provider = structure_factories.CustomerFactory()
        self.provider_owner = structure_factories.UserFactory()
        self.provider.add_user(self.provider_owner, CustomerRole.OWNER)
        self.other = structure_factories.CustomerFactory()

    def create(self, user, customer):
        self.client.force_authenticate(user)
        return self.client.post(
            reverse("openportal-project-template-list"),
            {
                "name": "template",
                "portal": "portal",
                "provider": structure_factories.CustomerFactory.get_url(self.provider),
                "customer": structure_factories.CustomerFactory.get_url(customer),
                "offerings": [],
            },
            format="json",
        )

    def support_provider_owner(self):
        user = structure_factories.UserFactory(is_support=True)
        self.provider.add_user(user, CustomerRole.OWNER)
        return user

    def test_global_support_owning_the_provider_cannot_place_projects_in_an_unrelated_organization(
        self,
    ):
        response = self.create(self.support_provider_owner(), self.other)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("customer", response.data)
        self.assertFalse(models.ProjectTemplate.objects.exists())

    def test_global_support_owning_the_provider_places_projects_in_it(self):
        response = self.create(self.support_provider_owner(), self.provider)
        self.assertEqual(response.status_code, 201, response.data)

    def test_global_support_owning_the_provider_cannot_move_a_template_to_an_unrelated_organization(
        self,
    ):
        template = models.ProjectTemplate.objects.create(
            name="template",
            portal="portal",
            provider=self.provider,
            customer=self.provider,
        )
        self.client.force_authenticate(self.support_provider_owner())
        response = self.client.patch(
            reverse(
                "openportal-project-template-detail",
                kwargs={"uuid": template.uuid.hex},
            ),
            {"customer": structure_factories.CustomerFactory.get_url(self.other)},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("customer", response.data)
        template.refresh_from_db()
        self.assertEqual(template.customer, self.provider)

    def test_provider_owner_cannot_place_projects_in_an_unrelated_organization(self):
        response = self.create(self.provider_owner, self.other)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("customer", response.data)
        self.assertFalse(models.ProjectTemplate.objects.exists())

    def test_provider_owner_places_projects_in_their_own_organization(self):
        response = self.create(self.provider_owner, self.provider)
        self.assertEqual(response.status_code, 201, response.data)

    def test_provider_owner_places_projects_in_an_organization_they_hold_a_role_in(
        self,
    ):
        self.other.add_user(self.provider_owner, CustomerRole.OWNER)
        response = self.create(self.provider_owner, self.other)
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_project_role_in_the_organization_is_enough(self):
        # The same rule as updating a template: an organization the caller's
        # roles reach, including through one of its projects.
        project = structure_factories.ProjectFactory(customer=self.other)
        project.add_user(self.provider_owner, ProjectRole.MEMBER)
        response = self.create(self.provider_owner, self.other)
        self.assertEqual(response.status_code, 201, response.data)

    def test_staff_places_projects_in_any_organization(self):
        staff = structure_factories.UserFactory(is_staff=True)
        response = self.create(staff, self.other)
        self.assertEqual(response.status_code, 201, response.data)
        template = models.ProjectTemplate.objects.get()
        self.assertEqual(template.customer, self.other)

    def test_a_user_who_sees_but_does_not_own_the_provider_cannot_create_one(self):
        user = structure_factories.UserFactory()
        self.provider.add_user(user, CustomerRole.SUPPORT)
        response = self.create(user, self.provider)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(models.ProjectTemplate.objects.exists())

    def test_a_provider_the_caller_does_not_see_is_not_found(self):
        user = structure_factories.UserFactory()
        self.other.add_user(user, CustomerRole.OWNER)
        response = self.create(user, self.other)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("provider", response.data)
        self.assertFalse(models.ProjectTemplate.objects.exists())
