"""Associations and per-user usage are shown only to whom their allocation is (#1005).

An association names the local account a user has on an allocation, and the
allocation user usage reports what that account consumed. Both are scoped
through their allocation's project, as the allocation itself is: a user with no
role on the project or its organization sees neither.
"""

from ddt import data, ddt
from django.urls import reverse
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import models


@ddt
class AssociationVisibilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        # The provider's settings belong to another organization: visibility
        # follows the allocation's project, not its service settings.
        settings = structure_factories.ServiceSettingsFactory(
            customer=structure_factories.CustomerFactory()
        )
        allocation = models.Allocation.objects.create(
            name="allocation",
            project=self.fixture.project,
            service_settings=settings,
        )
        remote_allocation = models.RemoteAllocation.objects.create(
            name="remote allocation",
            project=self.fixture.project,
            service_settings=settings,
        )
        member = self.fixture.admin
        self.objects = {
            "openportal-association": models.Association.objects.create(
                allocation=allocation, user=member, username="secretuser"
            ),
            "openportal-remote-association": models.RemoteAssociation.objects.create(
                allocation=remote_allocation, user=member
            ),
            "openportal-allocation-user-usage": models.AllocationUserUsage.objects.create(
                allocation=allocation,
                user=member,
                username="secretuser",
                year=2026,
                month=10,
            ),
        }

    def detail_url(self, basename, obj):
        lookup = {"uuid": obj.uuid.hex} if hasattr(obj, "uuid") else {"pk": obj.pk}
        return reverse(f"{basename}-detail", kwargs=lookup)

    def listed(self, basename, user):
        self.client.force_authenticate(user)
        response = self.client.get(reverse(f"{basename}-list"))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def detail_status(self, basename, user):
        self.client.force_authenticate(user)
        url = self.detail_url(basename, self.objects[basename])
        return self.client.get(url).status_code

    @data(
        "openportal-association",
        "openportal-remote-association",
        "openportal-allocation-user-usage",
    )
    def test_hidden_from_user_without_a_role(self, basename):
        outsider = structure_factories.UserFactory()
        self.assertEqual(self.listed(basename, outsider), [])
        self.assertEqual(self.detail_status(basename, outsider), 404)

    @data(
        "openportal-association",
        "openportal-remote-association",
        "openportal-allocation-user-usage",
    )
    def test_hidden_from_another_organization(self, basename):
        other = structure_fixtures.ProjectFixture()
        for user in (other.owner, other.admin):
            self.assertEqual(self.listed(basename, user), [])
            self.assertEqual(self.detail_status(basename, user), 404)

    @data(
        "openportal-association",
        "openportal-remote-association",
        "openportal-allocation-user-usage",
    )
    def test_shown_to_project_and_organization_roles_staff_and_support(self, basename):
        for user in (
            self.fixture.admin,
            self.fixture.manager,
            self.fixture.member,
            self.fixture.owner,
            self.fixture.staff,
            self.fixture.global_support,
        ):
            self.assertEqual(len(self.listed(basename, user)), 1, user)
            self.assertEqual(self.detail_status(basename, user), 200, user)
