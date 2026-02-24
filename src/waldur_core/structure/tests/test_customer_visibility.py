from urllib.parse import urlencode

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories
from waldur_mastermind.billing import models as billing_models


class CustomerVisibilityBaseTest(test.APITestCase):
    """Base test class that sets up a customer with 3 projects and various users."""

    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.project1 = factories.ProjectFactory(
            customer=self.customer, name="Project1"
        )
        self.project2 = factories.ProjectFactory(
            customer=self.customer, name="Project2"
        )
        self.project3 = factories.ProjectFactory(
            customer=self.customer, name="Project3"
        )

        # User with project-level role on project1 only
        self.project_admin = factories.UserFactory()
        self.project1.add_user(self.project_admin, ProjectRole.ADMIN)

        # Owner with customer-level role
        self.owner = factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)

        # Staff user
        self.staff = factories.UserFactory(is_staff=True)

    def _get_list_url(self, fields=None):
        url = factories.CustomerFactory.get_list_url()
        if fields:
            # Always include uuid so we can identify customers in the response
            all_fields = list(set(["uuid"] + fields))
            query_string = urlencode({"field": all_fields}, doseq=True)
            url += f"?{query_string}" if "?" not in url else f"&{query_string}"
        return url

    def _get_detail_url(self, customer=None):
        if customer is None:
            customer = self.customer
        return factories.CustomerFactory.get_url(customer)

    def _get_customer_data(self, user, fields=None, detail=False):
        self.client.force_authenticate(user=user)
        if detail:
            response = self.client.get(self._get_detail_url())
        else:
            url = self._get_list_url(fields=fields)
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if detail:
            return response.data
        # Find our customer in the list
        for item in response.data:
            if item["uuid"] == self.customer.uuid.hex:
                return item
        return None


class ProjectsCountVisibilityTest(CustomerVisibilityBaseTest):
    def test_project_admin_sees_only_their_project_counted(self):
        data = self._get_customer_data(self.project_admin, fields=["projects_count"])
        self.assertIsNotNone(data)
        self.assertEqual(data["projects_count"], 1)

    def test_owner_sees_all_projects_counted(self):
        data = self._get_customer_data(self.owner, fields=["projects_count"])
        self.assertIsNotNone(data)
        self.assertEqual(data["projects_count"], 3)

    def test_staff_sees_all_projects_counted(self):
        data = self._get_customer_data(self.staff, fields=["projects_count"])
        self.assertIsNotNone(data)
        self.assertEqual(data["projects_count"], 3)


class UsersCountVisibilityTest(CustomerVisibilityBaseTest):
    def setUp(self):
        super().setUp()
        # Add extra users to other projects
        self.user_in_project2 = factories.UserFactory()
        self.project2.add_user(self.user_in_project2, ProjectRole.MEMBER)
        self.user_in_project3 = factories.UserFactory()
        self.project3.add_user(self.user_in_project3, ProjectRole.MEMBER)

    def test_project_admin_sees_users_count_scoped_to_their_project(self):
        data = self._get_customer_data(self.project_admin, fields=["users_count"])
        self.assertIsNotNone(data)
        # Only project_admin is in project1
        self.assertEqual(data["users_count"], 1)

    def test_owner_sees_all_users_count(self):
        data = self._get_customer_data(self.owner, fields=["users_count"])
        self.assertIsNotNone(data)
        # owner (customer role) + project_admin + user_in_project2 + user_in_project3
        self.assertEqual(data["users_count"], 4)

    def test_staff_sees_all_users_count(self):
        data = self._get_customer_data(self.staff, fields=["users_count"])
        self.assertIsNotNone(data)
        self.assertEqual(data["users_count"], 4)


class BillingEstimateVisibilityTest(CustomerVisibilityBaseTest):
    def setUp(self):
        super().setUp()
        # Update existing price estimates (auto-created by signal handler)
        project_ct = ContentType.objects.get_for_model(self.project1)
        customer_ct = ContentType.objects.get_for_model(self.customer)

        billing_models.PriceEstimate.objects.filter(
            content_type=project_ct, object_id=self.project1.id
        ).update(total=100.0)
        billing_models.PriceEstimate.objects.filter(
            content_type=project_ct, object_id=self.project2.id
        ).update(total=200.0)
        billing_models.PriceEstimate.objects.filter(
            content_type=project_ct, object_id=self.project3.id
        ).update(total=300.0)
        # Customer-level estimate (sum of all projects)
        billing_models.PriceEstimate.objects.filter(
            content_type=customer_ct, object_id=self.customer.id
        ).update(total=600.0)

    def test_project_admin_sees_estimate_from_their_project_only(self):
        data = self._get_customer_data(
            self.project_admin, fields=["billing_price_estimate"]
        )
        self.assertIsNotNone(data)
        estimate = data["billing_price_estimate"]
        # Should see only project1's total (100.0)
        self.assertAlmostEqual(float(estimate["total"]), 100.0, places=1)

    def test_owner_sees_full_customer_estimate(self):
        data = self._get_customer_data(self.owner, fields=["billing_price_estimate"])
        self.assertIsNotNone(data)
        estimate = data["billing_price_estimate"]
        # Should see customer-level total (600.0)
        self.assertAlmostEqual(float(estimate["total"]), 600.0, places=1)

    def test_staff_sees_full_customer_estimate(self):
        data = self._get_customer_data(self.staff, fields=["billing_price_estimate"])
        self.assertIsNotNone(data)
        estimate = data["billing_price_estimate"]
        self.assertAlmostEqual(float(estimate["total"]), 600.0, places=1)


class DetailViewVisibilityTest(CustomerVisibilityBaseTest):
    def setUp(self):
        super().setUp()
        # Add extra users to other projects
        self.user_in_project2 = factories.UserFactory()
        self.project2.add_user(self.user_in_project2, ProjectRole.MEMBER)

    def test_detail_view_project_admin_sees_restricted_projects_count(self):
        data = self._get_customer_data(self.project_admin, detail=True)
        self.assertEqual(data["projects_count"], 1)

    def test_detail_view_owner_sees_all_projects_count(self):
        data = self._get_customer_data(self.owner, detail=True)
        self.assertEqual(data["projects_count"], 3)

    def test_detail_view_staff_sees_all_projects_count(self):
        data = self._get_customer_data(self.staff, detail=True)
        self.assertEqual(data["projects_count"], 3)


class TerminatedProjectCustomerVisibilityTest(test.APITestCase):
    """Test that a user with a role only in a terminated project cannot see the customer."""

    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.user = factories.UserFactory()
        self.project.add_user(self.user, ProjectRole.ADMIN)

    def test_user_cannot_see_customer_after_only_project_is_terminated(self):
        # Terminate the project (soft delete)
        self.project.delete()

        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        customer_uuids = [item["uuid"] for item in response.data]
        self.assertNotIn(self.customer.uuid.hex, customer_uuids)

    def test_user_can_see_customer_when_project_is_active(self):
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        customer_uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.customer.uuid.hex, customer_uuids)

    def test_user_sees_customer_if_another_active_project_exists(self):
        # User has roles in two projects under the same customer
        project2 = factories.ProjectFactory(customer=self.customer)
        project2.add_user(self.user, ProjectRole.ADMIN)

        # Terminate only the first project
        self.project.delete()

        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        customer_uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.customer.uuid.hex, customer_uuids)


class MixedRolesVisibilityTest(test.APITestCase):
    """Test user with customer-level role on one org and project-level role on another."""

    def setUp(self):
        self.user = factories.UserFactory()

        # Customer A: user has customer-level role (full visibility)
        self.customer_a = factories.CustomerFactory(name="Customer A")
        self.project_a1 = factories.ProjectFactory(
            customer=self.customer_a, name="A-Project1"
        )
        self.project_a2 = factories.ProjectFactory(
            customer=self.customer_a, name="A-Project2"
        )
        self.customer_a.add_user(self.user, CustomerRole.OWNER)

        # Customer B: user has only project-level role (restricted visibility)
        self.customer_b = factories.CustomerFactory(name="Customer B")
        self.project_b1 = factories.ProjectFactory(
            customer=self.customer_b, name="B-Project1"
        )
        self.project_b2 = factories.ProjectFactory(
            customer=self.customer_b, name="B-Project2"
        )
        self.project_b1.add_user(self.user, ProjectRole.ADMIN)

    def test_full_visibility_on_customer_a(self):
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_list_url()
        fields = urlencode({"field": ["uuid", "projects_count"]}, doseq=True)
        response = self.client.get(f"{url}?{fields}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        customer_a_data = None
        for item in response.data:
            if item["uuid"] == self.customer_a.uuid.hex:
                customer_a_data = item
                break

        self.assertIsNotNone(customer_a_data)
        self.assertEqual(customer_a_data["projects_count"], 2)

    def test_restricted_visibility_on_customer_b(self):
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_list_url()
        fields = urlencode({"field": ["uuid", "projects_count"]}, doseq=True)
        response = self.client.get(f"{url}?{fields}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        customer_b_data = None
        for item in response.data:
            if item["uuid"] == self.customer_b.uuid.hex:
                customer_b_data = item
                break

        self.assertIsNotNone(customer_b_data)
        self.assertEqual(customer_b_data["projects_count"], 1)
