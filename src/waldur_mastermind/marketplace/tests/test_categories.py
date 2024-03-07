from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests.helpers import override_marketplace_settings

from . import factories


@ddt
class CategoryGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.category = factories.CategoryFactory()
        self.category_url = factories.CategoryFactory.get_url(self.category)

    @data("staff", "owner", "user", "customer_support", "admin", "manager")
    def test_category_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.category_url = factories.CategoryFactory.get_url(self.category)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_category_should_be_invisible_to_unauthenticated_users(self):
        url = factories.CategoryFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_anonymous_user_can_see_category_list(self):
        url = factories.CategoryFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@ddt
class CategoryOfferingCountTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.category = factories.CategoryFactory()
        self.category_url = factories.CategoryFactory.get_url(self.category)
        self.share_offering = factories.OfferingFactory(
            shared=True, category=self.category, state=models.Offering.States.ACTIVE
        )
        self.private_offering = factories.OfferingFactory(
            shared=False,
            category=self.category,
            state=models.Offering.States.ACTIVE,
            customer=self.fixture.customer,
            project=self.fixture.project,
        )
        self.organization_group = structure_factories.OrganizationGroupFactory()

    def check_counts(self, offering_count):
        response = self.client.get(self.category_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["offering_count"], offering_count)

    def _create_plan(self, offering_count):
        self.plan = factories.PlanFactory(offering=self.private_offering)
        self.check_counts(offering_count)

    def _match_plan_with_organization_group(self, offering_count):
        self.plan.organization_groups.add(self.organization_group)
        self.check_counts(offering_count)

    def _match_customer_with_organization_group(self, offering_count):
        self.fixture.customer.organization_group = self.organization_group
        self.fixture.customer.save()
        self.check_counts(offering_count)

    def _match_project_with_organization_group(self, offering_count):
        self.fixture.project.organization_group = self.organization_group
        self.fixture.project.save()
        self.check_counts(offering_count)

    def _create_offering_for_owner(self, offering_count):
        factories.OfferingFactory(
            shared=False,
            state=models.Offering.States.ACTIVE,
            category=self.category,
            customer=self.fixture.customer,
            project=self.fixture.project,
        )
        self.check_counts(offering_count)

    @data("staff", "global_support")
    def test_counts_for_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        self.check_counts(offering_count=1)
        self._create_plan(offering_count=2)
        self._match_plan_with_organization_group(offering_count=2)
        self._match_customer_with_organization_group(offering_count=2)
        self._match_project_with_organization_group(offering_count=2)
        self._create_offering_for_owner(offering_count=2)

    @data("owner", "admin", "manager")
    def test_counts_for_authorized_user(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        self.check_counts(offering_count=1)
        self._create_plan(offering_count=2)
        self._match_plan_with_organization_group(offering_count=1)
        self._match_customer_with_organization_group(offering_count=2)
        self._match_project_with_organization_group(offering_count=2)
        self._create_offering_for_owner(offering_count=2)

    def test_counts_for_user(self):
        user = self.fixture.user
        self.client.force_authenticate(user)

        self.check_counts(offering_count=1)
        self._create_plan(offering_count=1)
        self._match_plan_with_organization_group(offering_count=1)
        self._match_customer_with_organization_group(offering_count=1)
        self._match_project_with_organization_group(offering_count=1)
        self._create_offering_for_owner(offering_count=1)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_counts_for_anonymous(self):
        self.check_counts(offering_count=1)
        self._create_plan(offering_count=1)
        self._match_plan_with_organization_group(offering_count=1)
        self._match_customer_with_organization_group(offering_count=1)
        self._match_project_with_organization_group(offering_count=1)
        self._create_offering_for_owner(offering_count=1)

    @override_marketplace_settings(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_counts_for_anonymous_if_anonymous_cannot_view_offerings(self):
        response = self.client.get(self.category_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_do_not_increment_the_counter_if_there_are_multiple_plans(self):
        user = self.fixture.owner
        self.client.force_authenticate(user)

        self.check_counts(offering_count=1)
        factories.PlanFactory(offering=self.private_offering)
        factories.PlanFactory(offering=self.private_offering)
        self.check_counts(offering_count=2)

        factories.PlanFactory(offering=self.share_offering)
        factories.PlanFactory(offering=self.share_offering)
        self.check_counts(offering_count=2)


@ddt
class CategoryCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    @data(
        "staff",
    )
    def test_authorized_user_can_create_category(self, user):
        response = self.create_category(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Category.objects.filter(title="category").exists())

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_create_category(self, user):
        response = self.create_category(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_category(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryFactory.get_list_url()

        payload = {
            "title": "category",
        }

        return self.client.post(url, payload)


@ddt
class CategoryUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    @data(
        "staff",
    )
    def test_authorized_user_can_update_category(self, user):
        response, category = self.update_category(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(category.title, "new_category")
        self.assertTrue(models.Category.objects.filter(title="new_category").exists())

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_update_category(self, user):
        response, category = self.update_category(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_category(self, user):
        category = factories.CategoryFactory()
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryFactory.get_url(category)

        response = self.client.patch(url, {"title": "new_category"})
        category.refresh_from_db()

        return response, category


@ddt
class CategoryDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.category = factories.CategoryFactory(title="category")

    @data(
        "staff",
    )
    def test_authorized_user_can_delete_category(self, user):
        response = self.delete_category(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(models.Category.objects.filter(title="category").exists())

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_delete_category(self, user):
        response = self.delete_category(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Category.objects.filter(title="category").exists())

    def delete_category(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CategoryFactory.get_url(self.category)
        response = self.client.delete(url)
        return response
