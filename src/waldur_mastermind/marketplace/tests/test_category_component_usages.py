import datetime

from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories

LIST_URL = reverse("marketplace-category-component-usage-list")


def detail_url(usage):
    return reverse(
        "marketplace-category-component-usage-detail", kwargs={"pk": usage.pk}
    )


class CategoryComponentUsageVisibilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.other = structure_fixtures.ProjectFixture()
        component = factories.CategoryComponentFactory()

        def usage(scope, reported_usage):
            return models.CategoryComponentUsage.objects.create(
                scope=scope,
                component=component,
                date=datetime.date.today(),
                reported_usage=reported_usage,
            )

        self.customer_usage = usage(self.fixture.customer, 10)
        self.project_usage = usage(self.fixture.project, 20)
        self.other_customer_usage = usage(self.other.customer, 777)
        self.other_project_usage = usage(self.other.project, 888)

    def list_usages(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(LIST_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return sorted(row["reported_usage"] for row in response.data)

    def test_user_without_roles_sees_nothing(self):
        self.assertEqual(self.list_usages(structure_factories.UserFactory()), [])

    def test_user_without_roles_cannot_retrieve_usage(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(detail_url(self.other_customer_usage))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_sees_usage_of_their_organization_and_its_projects(self):
        self.assertEqual(self.list_usages(self.fixture.owner), [10, 20])

    def test_project_member_sees_usage_of_their_project_and_its_organization(self):
        # A project role reaches the project and, as the organization list
        # shows it, its organization.
        self.assertEqual(self.list_usages(self.fixture.admin), [10, 20])

    def test_staff_and_support_see_all(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            self.assertEqual(self.list_usages(user), [10, 20, 777, 888])

    def test_scope_the_caller_can_see_narrows_the_list(self):
        scope = structure_factories.ProjectFactory.get_url(self.fixture.project)
        self.assertEqual(self.list_usages(self.fixture.owner, scope=scope), [20])

    def test_scope_the_caller_cannot_see_gives_an_empty_list(self):
        scope = structure_factories.CustomerFactory.get_url(self.other.customer)
        self.assertEqual(self.list_usages(self.fixture.owner, scope=scope), [])

    def test_scope_the_caller_cannot_see_gives_404_on_retrieve(self):
        self.client.force_authenticate(self.fixture.owner)
        scope = structure_factories.CustomerFactory.get_url(self.other.customer)
        response = self.client.get(
            detail_url(self.other_customer_usage), {"scope": scope}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_scope_that_is_not_an_organization_or_project_url_is_an_error(self):
        self.client.force_authenticate(self.fixture.staff)
        for scope in ("garbage", factories.OfferingFactory.get_url()):
            with self.subTest(scope=scope):
                response = self.client.get(LIST_URL, {"scope": scope})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_anonymous_is_refused(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
