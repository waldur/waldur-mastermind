"""OpenPortal user and project info are shown only to whom the user or project is (#1016).

A user's shortname is their local account name, and a project's shortname
completes it, so each is shown only to whom the user or the project is
visible. Reading another user's or project's info does not create or sync it
for a caller who may not see them.
"""

from django.urls import reverse
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import models


class InfoVisibilityMixin:
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()

    def get(self, url, user):
        self.client.force_authenticate(user)
        return self.client.get(url)

    def put(self, url, user, data):
        self.client.force_authenticate(user)
        return self.client.put(url, data)

    def listed(self, user):
        response = self.get(reverse(f"{self.basename}-list"), user)
        self.assertEqual(response.status_code, 200, response.data)
        return {row["shortname"] for row in response.data}

    def assert_hidden(self, user):
        self.assertNotIn(self.shortname, self.listed(user))
        self.assertEqual(self.get(self.detail_url(), user).status_code, 404)

    def assert_shown(self, user):
        self.assertIn(self.shortname, self.listed(user))
        response = self.get(self.detail_url(), user)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["shortname"], self.shortname)

    def test_hidden_from_user_without_a_role(self):
        self.assert_hidden(structure_factories.UserFactory())

    def test_hidden_from_another_organization(self):
        other = structure_fixtures.ProjectFixture()
        self.assert_hidden(other.owner)
        self.assert_hidden(other.admin)

    def test_shown_to_staff_and_support(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            self.assert_shown(user)


class UserInfoVisibilityTest(InfoVisibilityMixin, test.APITestCase):
    basename = "openportal-userinfo"
    shortname = "victimshort"

    def setUp(self):
        super().setUp()
        self.target = self.fixture.admin
        models.UserInfo.objects.create(user=self.target, shortname=self.shortname)

    def detail_url(self, user=None):
        user = user or self.target
        return reverse("openportal-userinfo-detail", kwargs={"user": user.uuid.hex})

    def test_shown_to_the_user(self):
        self.assert_shown(self.target)

    def test_shown_to_users_who_see_the_user(self):
        for user in (self.fixture.owner, self.fixture.manager, self.fixture.member):
            self.assert_shown(user)

    def test_read_does_not_create_info_for_a_hidden_user(self):
        stranger = structure_factories.UserFactory()
        outsider = structure_factories.UserFactory()
        response = self.get(self.detail_url(stranger), outsider)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(models.UserInfo.objects.filter(user=stranger).exists())

    def test_set_shortname_is_refused_before_anything_is_written(self):
        # Only staff may PUT here (IsAdminOrReadOnly), whoever the user is.
        stranger = structure_factories.UserFactory()
        url = reverse(
            "openportal-userinfo-set-shortname", kwargs={"user": stranger.uuid.hex}
        )
        outsider = structure_factories.UserFactory()
        self.assertEqual(self.put(url, outsider, {"shortname": "x"}).status_code, 403)
        self.assertFalse(models.UserInfo.objects.filter(user=stranger).exists())
        url = reverse(
            "openportal-userinfo-set-shortname",
            kwargs={"user": self.fixture.member.uuid.hex},
        )
        response = self.put(url, self.fixture.owner, {"shortname": "x"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            models.UserInfo.objects.filter(user=self.fixture.member).exists()
        )


class ProjectInfoVisibilityTest(InfoVisibilityMixin, test.APITestCase):
    basename = "openportal-projectinfo"
    shortname = "victimproj"

    def setUp(self):
        super().setUp()
        models.ProjectInfo.objects.update_or_create(
            project=self.fixture.project, defaults={"shortname": self.shortname}
        )

    def detail_url(self, project=None):
        project = project or self.fixture.project
        return reverse(
            "openportal-projectinfo-detail", kwargs={"project": project.uuid.hex}
        )

    def test_shown_to_project_and_organization_roles(self):
        for user in (
            self.fixture.owner,
            self.fixture.admin,
            self.fixture.manager,
            self.fixture.member,
        ):
            self.assert_shown(user)

    def test_hidden_from_organization_roles_that_do_not_list_projects(self):
        # The project endpoint needs PROJECT.LIST, which a service-provider
        # manager of the organization lacks; so does its project info.
        user = self.fixture.service_manager
        projects = self.get(reverse("project-list"), user).data
        self.assertNotIn(self.fixture.project.uuid.hex, {p["uuid"] for p in projects})
        self.assert_hidden(user)

    def test_read_does_not_create_info_for_a_hidden_project(self):
        project = structure_factories.ProjectFactory()
        models.ProjectInfo.objects.filter(project=project).delete()
        outsider = structure_factories.UserFactory()
        self.assertEqual(self.get(self.detail_url(project), outsider).status_code, 404)
        self.assertFalse(models.ProjectInfo.objects.filter(project=project).exists())

    def test_staff_actions_are_refused_before_anything_is_written(self):
        # Both are staff-only, also for the organization owner.
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        models.ProjectInfo.objects.filter(project=project).delete()
        for action, data in (
            ("set-shortname", {"shortname": "x"}),
            ("set-allowed-destinations", {"allowed_destinations": "x"}),
        ):
            url = reverse(
                f"openportal-projectinfo-{action}",
                kwargs={"project": project.uuid.hex},
            )
            for user in (structure_factories.UserFactory(), self.fixture.owner):
                response = self.put(url, user, data)
                self.assertEqual(response.status_code, 403, action)
            self.assertFalse(
                models.ProjectInfo.objects.filter(project=project).exists(), action
            )
