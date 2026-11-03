"""Project templates and managed projects are shown only to their organizations (#1015).

A project template belongs to the provider organization that offers it and
names the organization its projects are placed in. A managed project follows
its template and the project it is attached to. They are shown to the owners
of those organizations and to the provider's service-provider managers; other
organization roles, members of the attached project and other organizations
see neither. The template key, shared with the remote portal, is shown only
to those who may edit the template.
"""

from django.urls import reverse
from rest_framework import test

from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openportal import models


class TemplateVisibilityMixin:
    def setUp(self):
        self.provider = structure_factories.CustomerFactory()
        self.provider_owner = structure_factories.UserFactory()
        self.provider.add_user(self.provider_owner, CustomerRole.OWNER)
        self.consumer = structure_fixtures.ProjectFixture()
        self.template = models.ProjectTemplate.objects.create(
            name="template",
            portal="portal",
            key="shared-key",
            provider=self.provider,
            customer=self.consumer.customer,
        )
        # Attached to a project of a third organization, so each path is
        # tested on its own.
        self.attached = structure_fixtures.ProjectFixture()
        self.managed_project = models.ManagedProject.objects.create(
            destination="destination",
            identifier="identifier",
            project_template=self.template,
            project=self.attached.project,
            # A remote portal's award carries the template key it matched.
            details={"name": "details", "key": "shared-key"},
        )

    def get(self, url, user):
        self.client.force_authenticate(user)
        return self.client.get(url)

    def listed(self, user):
        response = self.get(reverse(f"{self.basename}-list"), user)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def assert_hidden(self, user):
        self.assertEqual(self.listed(user), [])
        self.assertEqual(self.get(self.detail_url(), user).status_code, 404)

    def assert_shown(self, user):
        self.assertEqual(len(self.listed(user)), 1)
        response = self.get(self.detail_url(), user)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def key_seen_by(self, user):
        return self.template_data(self.assert_shown(user))["key"]

    def service_provider_manager(self):
        service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.provider
        )
        manager = structure_factories.UserFactory()
        service_provider.add_user(manager, ServiceProviderRole.MANAGER)
        return manager

    def test_hidden_from_user_without_a_role(self):
        self.assert_hidden(structure_factories.UserFactory())

    def test_hidden_from_another_organization(self):
        other = structure_fixtures.ProjectFixture()
        self.assert_hidden(other.owner)
        self.assert_hidden(other.admin)

    def test_shown_to_staff_and_support(self):
        for user in (self.consumer.staff, self.consumer.global_support):
            self.assert_shown(user)

    def test_shown_to_provider_organization(self):
        self.assert_shown(self.provider_owner)

    def test_shown_to_organization_projects_are_placed_in(self):
        self.assert_shown(self.consumer.owner)

    def test_shown_to_service_provider_managers(self):
        # Homeport sends them to these pages; the role sits on the service
        # provider, not on its organization.
        self.assert_shown(self.service_provider_manager())

    def test_hidden_from_other_organization_roles(self):
        # As from the managed project audit log: owners only.
        for customer in (self.provider, self.consumer.customer):
            user = structure_factories.UserFactory()
            customer.add_user(user, CustomerRole.SUPPORT)
            self.assert_hidden(user)

    def test_key_is_shown_only_to_those_who_may_edit_the_template(self):
        self.assertEqual(self.key_seen_by(self.provider_owner), "shared-key")
        self.assertEqual(self.key_seen_by(self.consumer.staff), "shared-key")
        for user in (
            self.consumer.owner,
            self.consumer.global_support,
            self.service_provider_manager(),
        ):
            self.assertIsNone(self.key_seen_by(user))


class ProjectTemplateVisibilityTest(TemplateVisibilityMixin, test.APITestCase):
    basename = "openportal-project-template"

    def template_data(self, data):
        return data

    def detail_url(self):
        return reverse(
            "openportal-project-template-detail",
            kwargs={"uuid": self.template.uuid.hex},
        )

    def test_hidden_from_members_of_a_managed_project(self):
        # Attaching a project gives its members no view of the template.
        self.assert_hidden(self.attached.admin)
        self.assert_hidden(self.attached.owner)


class ManagedProjectVisibilityTest(TemplateVisibilityMixin, test.APITestCase):
    basename = "openportal-managed-project"

    def template_data(self, data):
        return data["project_template_data"]

    def detail_url(self):
        return reverse(
            "openportal-managed-project-retrieve-custom",
            kwargs={
                "identifier": self.managed_project.identifier,
                "destination": self.managed_project.destination,
            },
        )

    def test_shown_to_attached_project_organization(self):
        self.assert_shown(self.attached.owner)
        # The template nested in it does not carry the key.
        self.assertIsNone(self.key_seen_by(self.attached.owner))

    def test_award_key_is_shown_only_to_those_who_may_edit_the_template(self):
        for user in (self.provider_owner, self.consumer.staff):
            self.assertEqual(self.assert_shown(user)["details"]["key"], "shared-key")
        for user in (
            self.attached.owner,
            self.consumer.owner,
            self.service_provider_manager(),
        ):
            details = self.assert_shown(user)["details"]
            self.assertIsNone(details["key"])
            self.assertEqual(details["name"], "details")

    def test_audit_entries_hide_the_award_key(self):
        models.ManagedProjectAuditEntry.record(
            self.managed_project,
            models.ManagedProjectAuditEventType.DELETED,
            previous_details=self.managed_project.details,
            new_details=self.managed_project.details,
        )
        url = reverse("openportal-managed-project-audit-list")
        for user, key in (
            (self.attached.owner, None),
            (self.consumer.staff, "shared-key"),
        ):
            entries = self.get(url, user).data
            self.assertTrue(entries, user)
            for entry in entries:
                for field in ("previous_details", "new_details"):
                    self.assertEqual(entry[field]["key"], key, field)
        # Nor can a search find it.
        self.client.force_authenticate(self.attached.owner)
        for q, count in (("shared-key", 0), ('"key"', 0), ("details", 1)):
            response = self.client.get(url, {"q": q})
            self.assertEqual(len(response.data), count, q)

    def test_hidden_from_attached_project_members(self):
        # The audit log keeps project members out too: details may be
        # privileged.
        for user in (self.attached.admin, self.attached.manager):
            self.assert_hidden(user)

    def approve(self, user):
        url = reverse(
            "openportal-managed-project-approve",
            kwargs={
                "identifier": self.managed_project.identifier,
                "destination": self.managed_project.destination,
            },
        )
        self.client.force_authenticate(user)
        return self.client.post(url, {"comment": "x"}).status_code

    def test_actions_do_not_find_it_for_an_outsider(self):
        self.assertEqual(self.approve(structure_factories.UserFactory()), 404)

    def test_seeing_it_does_not_allow_acting_on_it(self):
        # Approving needs the provider's owner or manager together with the
        # customer's owner; each of these sees the managed project only.
        for user in (
            self.attached.owner,
            self.consumer.owner,
            self.provider_owner,
            self.service_provider_manager(),
        ):
            self.assertEqual(self.approve(user), 403)
