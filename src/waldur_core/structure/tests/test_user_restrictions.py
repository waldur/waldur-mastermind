from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories
from waldur_core.structure.tests.utils import client_add_user


class CustomerUserRestrictionsTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.staff = factories.UserFactory(is_staff=True)
        self.customer = factories.CustomerFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)

    def test_user_with_matching_email_can_be_added_to_customer(self):
        self.customer.user_email_patterns = [".*@example.com"]
        self.customer.save()

        target_user = factories.UserFactory(email="user@example.com")
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_with_non_matching_email_cannot_be_added_to_customer(self):
        self.customer.user_email_patterns = [".*@example.com"]
        self.customer.save()

        target_user = factories.UserFactory(email="user@other.com")
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("restrictions", str(response.data))

    def test_user_with_matching_affiliation_can_be_added_to_customer(self):
        self.customer.user_affiliations = ["staff", "student"]
        self.customer.save()

        target_user = factories.UserFactory(affiliations=["staff"])
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_with_non_matching_affiliation_cannot_be_added_to_customer(self):
        self.customer.user_affiliations = ["staff", "student"]
        self.customer.save()

        target_user = factories.UserFactory(affiliations=["guest"])
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("restrictions", str(response.data))

    def test_user_with_no_restrictions_can_be_added_to_customer(self):
        # No restrictions set - all users allowed
        target_user = factories.UserFactory()
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_user_is_also_restricted(self):
        self.customer.user_email_patterns = [".*@example.com"]
        self.customer.save()

        # Staff user with non-matching email should be blocked
        staff_target = factories.UserFactory(is_staff=True, email="staff@other.com")
        response = client_add_user(
            self.client, self.staff, staff_target, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_email_or_affiliation_match_allows_user(self):
        # Set both email patterns and affiliations - OR logic within each
        self.customer.user_email_patterns = [".*@example.com"]
        self.customer.user_affiliations = ["staff"]
        self.customer.save()

        # User matches affiliation but not email - should be allowed
        target_user = factories.UserFactory(
            email="user@other.com", affiliations=["staff"]
        )
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_with_matching_identity_source_can_be_added_to_customer(self):
        self.customer.user_identity_sources = ["eduGAIN", "SAML"]
        self.customer.save()

        target_user = factories.UserFactory(identity_source="eduGAIN")
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_with_non_matching_identity_source_cannot_be_added_to_customer(self):
        self.customer.user_identity_sources = ["eduGAIN", "SAML"]
        self.customer.save()

        target_user = factories.UserFactory(identity_source="local")
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("restrictions", str(response.data))

    def test_identity_source_or_email_match_allows_user(self):
        # Set both identity sources and email patterns - OR logic
        self.customer.user_identity_sources = ["eduGAIN"]
        self.customer.user_email_patterns = [".*@example.com"]
        self.customer.save()

        # User matches identity source but not email - should be allowed
        target_user = factories.UserFactory(
            email="user@other.com", identity_source="eduGAIN"
        )
        response = client_add_user(
            self.client, self.staff, target_user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class ProjectUserRestrictionsTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.staff = factories.UserFactory(is_staff=True)
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)

    def test_user_with_matching_email_can_be_added_to_project(self):
        self.project.user_email_patterns = [".*@example.com"]
        self.project.save()

        target_user = factories.UserFactory(email="user@example.com")
        response = client_add_user(
            self.client, self.staff, target_user, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_with_non_matching_email_cannot_be_added_to_project(self):
        self.project.user_email_patterns = [".*@example.com"]
        self.project.save()

        target_user = factories.UserFactory(email="user@other.com")
        response = client_add_user(
            self.client, self.staff, target_user, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("restrictions", str(response.data))

    def test_project_inherits_customer_restrictions(self):
        # Customer has restrictions, project has none
        self.customer.user_email_patterns = [".*@example.com"]
        self.customer.save()

        target_user = factories.UserFactory(email="user@other.com")
        response = client_add_user(
            self.client, self.staff, target_user, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("customer", str(response.data).lower())

    def test_user_must_match_both_customer_and_project_restrictions(self):
        # Both customer and project have restrictions
        self.customer.user_email_patterns = [".*@example.com"]
        self.customer.save()
        self.project.user_affiliations = ["staff"]
        self.project.save()

        # User matches customer email but not project affiliation
        target_user = factories.UserFactory(
            email="user@example.com", affiliations=["guest"]
        )
        response = client_add_user(
            self.client, self.staff, target_user, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_matching_both_customer_and_project_restrictions_allowed(self):
        # Both customer and project have restrictions
        self.customer.user_email_patterns = [".*@example.com"]
        self.customer.save()
        self.project.user_affiliations = ["staff"]
        self.project.save()

        # User matches both
        target_user = factories.UserFactory(
            email="user@example.com", affiliations=["staff"]
        )
        response = client_add_user(
            self.client, self.staff, target_user, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class CustomerRestrictionPermissionTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.staff = factories.UserFactory(is_staff=True)
        self.owner = factories.UserFactory()
        self.customer = factories.CustomerFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        # Owner needs UPDATE permission to access the endpoint
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)

    def test_staff_can_set_customer_restrictions(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            factories.CustomerFactory.get_url(self.customer),
            {"user_email_patterns": [".*@example.com"]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.user_email_patterns, [".*@example.com"])

    def test_non_staff_cannot_set_customer_restrictions(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.patch(
            factories.CustomerFactory.get_url(self.customer),
            {"user_email_patterns": [".*@example.com"]},
        )
        # Non-staff can access endpoint but can't modify staff_only_fields - field should be ignored
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        # Field should not be changed (read-only for non-staff)
        self.assertEqual(self.customer.user_email_patterns, [])


class ProjectRestrictionPermissionTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.staff = factories.UserFactory(is_staff=True)
        self.owner = factories.UserFactory()
        self.customer = factories.CustomerFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.project = factories.ProjectFactory(customer=self.customer)
        # Owner needs CREATE_PROJECT and UPDATE_PROJECT permissions
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        # Add owner to the project so they can see it
        self.project.add_user(self.owner, ProjectRole.ADMIN)

    def test_user_with_create_project_permission_can_set_project_restrictions(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.patch(
            factories.ProjectFactory.get_url(self.project),
            {"user_email_patterns": [".*@example.com"]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertEqual(self.project.user_email_patterns, [".*@example.com"])

    def test_user_without_create_project_permission_cannot_set_project_restrictions(
        self,
    ):
        # Create a project admin who has UPDATE_PROJECT but not CREATE_PROJECT
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)
        ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_PROJECT)

        self.client.force_authenticate(user=admin)
        response = self.client.patch(
            factories.ProjectFactory.get_url(self.project),
            {"user_email_patterns": [".*@example.com"]},
        )
        # Admin can access endpoint but can't modify restriction fields
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        # Field should not be changed (read-only for users without CREATE_PROJECT)
        self.assertEqual(self.project.user_email_patterns, [])
