import datetime
from decimal import Decimal
from unittest import mock
from urllib.parse import urlencode

from ddt import data, ddt
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.core.pagination import RESULT_COUNT_HEADER
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.quotas.models import QuotaUsage
from waldur_core.structure.handlers import update_customer_users_count
from waldur_core.structure.models import AccessSubnet, Customer, Project
from waldur_core.structure.tests import factories, fixtures
from waldur_core.structure.tests.utils import (
    client_add_user,
    client_delete_user,
    client_update_user,
)
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class CustomerBaseTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_PROJECTS)

    def _get_customer_url(self, customer, fields=None):
        url = "http://testserver" + reverse(
            "customer-detail", kwargs={"uuid": customer.uuid.hex}
        )
        if fields is not None:
            query_string = urlencode({"field": fields}, doseq=True)
            url += f"?{query_string}"
        return url

    def _get_customer_contact_url(self, customer):
        return "http://testserver" + reverse(
            "customer-contact", kwargs={"uuid": customer.uuid.hex}
        )

    def _get_project_url(self, project):
        return "http://testserver" + reverse(
            "project-detail", kwargs={"uuid": project.uuid.hex}
        )

    def _get_user_url(self, user):
        return "http://testserver" + reverse(
            "user-detail", kwargs={"uuid": user.uuid.hex}
        )


@freeze_time("2017-11-01")
class CustomerUserTest(CustomerBaseTest):
    def setUp(self):
        super().setUp()
        self.customer = factories.CustomerFactory()
        self.user = factories.UserFactory()
        self.created_by = factories.UserFactory()

    def test_add_user_returns_membership(self):
        permission = self.customer.add_user(self.user, CustomerRole.OWNER)

        self.assertEqual(permission.user, self.user)
        self.assertEqual(permission.scope, self.customer)

    def test_get_users_returns_empty_list(self):
        self.assertEqual(0, self.customer.get_users().count())


@ddt
class CustomerListTest(CustomerBaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.ProjectFixture()

    # List filtration tests
    @data(
        "staff",
        "global_support",
        "owner",
        "customer_support",
        "admin",
        "manager",
        "member",
    )
    def test_user_can_list_customers(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        self._check_user_list_access_customers(self.fixture.customer, "assertIn")

    @data("user", "admin", "manager", "member")
    def test_user_cannot_list_other_customer(self, user):
        customer = factories.CustomerFactory()
        self.client.force_authenticate(user=getattr(self.fixture, user))
        self._check_customer_in_list(customer, False)

    @data("staff", "global_support")
    def test_user_can_access_all_customers_if_he_is_staff(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        self._check_user_direct_access_customer(
            self.fixture.customer, status.HTTP_200_OK
        )

        customer = factories.CustomerFactory()
        self._check_user_direct_access_customer(customer, status.HTTP_200_OK)

    def test_filtering_customers_by_query(self):
        self.client.force_authenticate(user=self.fixture.staff)
        url = factories.CustomerFactory.get_list_url()
        customer_name = self.fixture.customer.name

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["projects_count"], 0)

        response = self.client.get(url, {"query": "abc"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        response = self.client.get(url, {"query": customer_name})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["projects_count"], 0)

    def test_filter_customers_by_current_user_has_project_create_permission(self):
        """Test that filter returns only customers where user has CREATE_PROJECT permission."""
        # Setup: Create customers and grant permission
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)

        customer_with_permission = factories.CustomerFactory()
        factories.CustomerFactory()
        factories.CustomerFactory()

        user = factories.UserFactory()
        customer_with_permission.add_user(user, CustomerRole.OWNER)

        # Authenticate as user and filter
        self.client.force_authenticate(user)
        url = factories.CustomerFactory.get_list_url()

        response = self.client.get(
            url, {"current_user_has_project_create_permission": "true"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], customer_with_permission.uuid.hex)

    def test_filter_customers_staff_sees_all_regardless_of_permission(self):
        """Test that staff users see all customers regardless of filter value."""
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)

        factories.CustomerFactory()
        factories.CustomerFactory()

        self.client.force_authenticate(self.fixture.staff)
        url = factories.CustomerFactory.get_list_url()

        # With filter=true, staff should see all
        response = self.client.get(
            url, {"current_user_has_project_create_permission": "true"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 2)

    # Helper methods
    def _check_user_list_access_customers(self, customer, test_function):
        response = self.client.get(reverse("customer-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        urls = set([instance["url"] for instance in response.data])
        url = self._get_customer_url(customer)
        getattr(self, test_function)(url, urls)

    def _check_customer_in_list(self, customer, positive=True):
        response = self.client.get(reverse("customer-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        urls = set([instance["url"] for instance in response.data])
        customer_url = self._get_customer_url(customer)
        if positive:
            self.assertIn(customer_url, urls)
        else:
            self.assertNotIn(customer_url, urls)

    def _check_user_direct_access_customer(self, customer, status_code):
        response = self.client.get(self._get_customer_url(customer))
        self.assertEqual(response.status_code, status_code)


@ddt
class CustomerDeleteTest(CustomerBaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.ProjectFixture()

    # Deletion tests
    @data(
        "owner",
        "admin",
        "manager",
        "global_support",
        "customer_support",
        "member",
    )
    def test_user_cannot_delete_customer(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.delete(self._get_customer_url(self.fixture.customer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_can_delete_customer_with_associated_projects_if_he_is_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)

        factories.ProjectFactory(customer=self.fixture.customer)
        response = self.client.delete(self._get_customer_url(self.fixture.customer))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_user_can_delete_customer_with_soft_deleted_projects(self):
        self.client.force_authenticate(user=self.fixture.staff)

        project = factories.ProjectFactory(customer=self.fixture.customer)

        # sof delete project
        project.delete()
        self.assertTrue(Project.objects.filter(id=project.id).exists())
        project.refresh_from_db()
        self.assertTrue(project.is_removed)

        response = self.client.delete(self._get_customer_url(self.fixture.customer))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class BaseCustomerMutationTest(CustomerBaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)

    # Helper methods
    def _get_valid_payload(self, resource=None):
        resource = resource or factories.CustomerFactory()

        return {
            "name": resource.name,
            "abbreviation": resource.abbreviation,
            "contact_details": resource.contact_details,
        }

    def _check_single_customer_field_change_permission(self, customer, status_code):
        payload = self._get_valid_payload(customer)

        for field, value in payload.items():
            data = {field: value}

            response = self.client.patch(self._get_customer_url(customer), data)
            self.assertEqual(response.status_code, status_code)


@ddt
class CustomerCreateTest(BaseCustomerMutationTest):
    @data("user", "global_support")
    def test_user_can_not_create_customer_if_he_is_not_staff(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_waldur_core_settings(CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION=True)
    def test_default_project_is_created_if_configured(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        customer = Customer.objects.get(uuid=response.data["uuid"])
        self.assertEqual(customer.projects.count(), 1)
        self.assertEqual(customer.projects.first().name, "First project")
        self.assertEqual(
            customer.projects.first().description,
            "First project we have created for you",
        )

    @override_waldur_core_settings(
        CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION=False
    )
    def test_default_project_is_not_created_if_configured(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        customer = Customer.objects.get(uuid=response.data["uuid"])
        self.assertEqual(customer.projects.count(), 0)

    def test_user_can_create_customer_if_he_is_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_domain_name_is_filled_from_input_for_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            factories.CustomerFactory.get_list_url(),
            {"name": "Computer Science Lab", "domain": "ut.ee"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["domain"], "ut.ee")


@ddt
class CustomerUpdateTest(BaseCustomerMutationTest):
    @data("manager", "admin", "customer_support", "member", "global_support")
    def test_user_cannot_change_customer_as_whole(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.put(
            self._get_customer_url(self.fixture.customer), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_cannot_change_customer_he_is_owner_of(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_CUSTOMER)
        self.client.force_authenticate(user=self.fixture.owner)

        response = self.client.put(
            self._get_customer_url(self.fixture.customer), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_can_change_customer_as_whole_if_he_is_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.put(
            self._get_customer_url(self.fixture.customer), self._get_valid_payload()
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            "Error message: %s" % response.data,
        )

    def test_user_cannot_change_single_customer_field_he_is_not_owner_of(self):
        self.client.force_authenticate(user=self.fixture.user)

        self._check_single_customer_field_change_permission(
            self.fixture.customer, status.HTTP_404_NOT_FOUND
        )

    def test_user_cannot_change_customer_field_he_is_owner_of(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_CUSTOMER)
        self.client.force_authenticate(user=self.fixture.owner)

        self._check_single_customer_field_change_permission(
            self.fixture.customer, status.HTTP_403_FORBIDDEN
        )

    def test_user_can_change_single_customer_field_if_he_is_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)
        self._check_single_customer_field_change_permission(
            self.fixture.customer, status.HTTP_200_OK
        )

    def test_staff_can_change_organization_domain(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.patch(
            self._get_customer_url(self.fixture.customer), {"domain": "ut.ee"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.domain, "ut.ee")

    def test_owner_can_not_change_organization_domain(self):
        self.client.force_authenticate(user=self.fixture.owner)

        response = self.client.patch(
            self._get_customer_url(self.fixture.customer), {"domain": "ut.ee"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.domain, "")

    def test_update_vat_code_with_valid_format(self):
        self.client.force_authenticate(user=self.fixture.staff)

        # Test with valid Austrian VAT number format
        response = self.client.patch(
            self._get_customer_url(self.fixture.customer),
            {"vat_code": "ATU99999999", "country": "AT"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.vat_code, "ATU99999999")

    def test_update_vat_code_with_invalid_format(self):
        self.client.force_authenticate(user=self.fixture.staff)

        # Test with invalid VAT number format
        response = self.client.patch(
            self._get_customer_url(self.fixture.customer),
            {"vat_code": "INVALID123", "country": "AT"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vat_code", response.data)

    def test_update_vat_code_comprehensive_validation(self):
        self.client.force_authenticate(user=self.fixture.staff)

        # Test various European VAT formats
        test_cases = [
            # Valid cases
            ("BE0123456789", "BE", True),
            ("DE123456789", "DE", True),
            ("FR1A123456789", "FR", True),
            ("NO123456789MVA", "NO", True),
            ("CHE123456789MWST", "CH", True),
            # Invalid cases should fail
            ("BE2123456789", "BE", False),  # Invalid first digit for Belgium
            ("DE12345678", "DE", False),  # Too short for Germany
            ("NO123456789", "NO", False),  # Missing MVA suffix for Norway
        ]

        for vat_code, country, should_succeed in test_cases:
            response = self.client.patch(
                self._get_customer_url(self.fixture.customer),
                {"vat_code": vat_code, "country": country},
            )

            if should_succeed:
                self.assertEqual(
                    response.status_code,
                    status.HTTP_200_OK,
                    f"VAT {vat_code} for {country} should be valid",
                )
                self.fixture.customer.refresh_from_db()
                self.assertEqual(self.fixture.customer.vat_code, vat_code)
            else:
                self.assertEqual(
                    response.status_code,
                    status.HTTP_400_BAD_REQUEST,
                    f"VAT {vat_code} for {country} should be invalid",
                )
                self.assertIn("vat_code", response.data)

    def test_staff_can_assign_project_metadata_checklist(self):
        """Test that staff users can assign a project metadata checklist to a customer."""
        # Create a PROJECT_METADATA checklist
        checklist = checklist_factories.ChecklistFactory(
            name="Project Metadata Checklist",
            checklist_type=ChecklistTypes.PROJECT_METADATA,
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.patch(
            self._get_customer_url(self.fixture.customer),
            {"project_metadata_checklist": str(checklist.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.project_metadata_checklist, checklist)

    def test_staff_can_unset_project_metadata_checklist(self):
        """Test that staff users can unset a project metadata checklist."""
        # Set up customer with a checklist
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROJECT_METADATA
        )
        self.fixture.customer.project_metadata_checklist = checklist
        self.fixture.customer.save()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.patch(
            self._get_customer_url(self.fixture.customer),
            {"project_metadata_checklist": None},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertIsNone(self.fixture.customer.project_metadata_checklist)

    def test_non_staff_cannot_assign_project_metadata_checklist(self):
        """Test that non-staff users cannot assign project metadata checklists."""
        checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROJECT_METADATA
        )

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.patch(
            self._get_customer_url(self.fixture.customer),
            {"project_metadata_checklist": str(checklist.uuid)},
        )

        # Should succeed but field should be read-only (ignored)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertIsNone(self.fixture.customer.project_metadata_checklist)

    def test_invalid_checklist_type_rejected(self):
        """Test that non-PROJECT_METADATA checklists are rejected."""
        # Create a different type of checklist
        wrong_checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROPOSAL_COMPLIANCE
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.patch(
            self._get_customer_url(self.fixture.customer),
            {"project_metadata_checklist": str(wrong_checklist.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_metadata_checklist", response.data)

    def test_nonexistent_checklist_rejected(self):
        """Test that invalid checklist UUIDs are rejected."""
        import uuid

        fake_uuid = str(uuid.uuid4())

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.patch(
            self._get_customer_url(self.fixture.customer),
            {"project_metadata_checklist": fake_uuid},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_metadata_checklist", response.data)


class CustomerContactUpdateTest(CustomerBaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.ProjectFixture()
        self.url = self._get_customer_contact_url(self.fixture.customer)
        self.payload = {
            "contact_details": "Updated contact details",
            "email": "contact@example.com",
            "phone_number": "+372000000",
            "homepage": "http://example.com",
            "notification_emails": "contact@example.com,alt@example.com",
        }

    def test_owner_can_update_contact_details_with_contact_permission(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_CUSTOMER)
        CustomerRole.OWNER.add_permission(PermissionEnum.CUSTOMER_CONTACT_UPDATE)
        self.client.force_authenticate(user=self.fixture.owner)

        response = self.client.post(self.url, self.payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.email, self.payload["email"])
        self.assertEqual(
            self.fixture.customer.contact_details, self.payload["contact_details"]
        )

    def test_owner_can_update_contact_details_with_update_permission(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)
        CustomerRole.OWNER.delete_permission(PermissionEnum.CUSTOMER_CONTACT_UPDATE)
        self.client.force_authenticate(user=self.fixture.owner)

        response = self.client.post(self.url, self.payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.email, self.payload["email"])

    def test_owner_cannot_update_contact_details_without_permissions(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.UPDATE_CUSTOMER)
        CustomerRole.OWNER.delete_permission(PermissionEnum.CUSTOMER_CONTACT_UPDATE)
        self.client.force_authenticate(user=self.fixture.owner)

        response = self.client.post(self.url, self.payload)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CustomerQuotasTest(test.APITestCase):
    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.staff = factories.UserFactory(is_staff=True)

    def test_customer_projects_quota_increases_on_project_creation(self):
        factories.ProjectFactory(customer=self.customer)
        self.assert_quota_usage("nc_project_count", 1)

    def test_customer_projects_quota_decreases_on_project_deletion(self):
        project = factories.ProjectFactory(customer=self.customer)
        project.delete()
        self.assert_quota_usage("nc_project_count", 0)

    def test_customer_users_quota_increases_on_adding_owner(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.OWNER)
        self.assert_quota_usage("nc_user_count", 1)

    def test_customer_users_quota_decreases_on_removing_owner(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.OWNER)
        self.customer.remove_user(user)
        self.assert_quota_usage("nc_user_count", 0)

    def test_customer_users_quota_increases_on_adding_administrator(self):
        project = factories.ProjectFactory(customer=self.customer)
        user = factories.UserFactory()
        project.add_user(user, ProjectRole.ADMIN)
        self.assert_quota_usage("nc_user_count", 1)

    def test_customer_users_quota_decreases_on_removing_administrator(self):
        project = factories.ProjectFactory(customer=self.customer)
        user = factories.UserFactory()
        project.add_user(user, ProjectRole.ADMIN)
        project.remove_user(user)
        self.assert_quota_usage("nc_user_count", 0)

    def test_customer_quota_is_not_increased_on_adding_owner_as_administrator(self):
        user = factories.UserFactory()
        project = factories.ProjectFactory(customer=self.customer)
        self.customer.add_user(user, CustomerRole.OWNER)
        project.add_user(user, ProjectRole.ADMIN)

        self.assert_quota_usage("nc_user_count", 1)

    def test_customer_quota_is_not_increased_on_adding_owner_as_manager(self):
        user = factories.UserFactory()
        project = factories.ProjectFactory(customer=self.customer)
        self.customer.add_user(user, CustomerRole.OWNER)
        project.add_user(user, ProjectRole.ADMIN)

        self.assert_quota_usage("nc_user_count", 1)

    def test_customer_users_quota_decreases_when_one_project_is_deleted(self):
        project = factories.ProjectFactory(customer=self.customer)
        user = factories.UserFactory()

        project.add_user(user, ProjectRole.ADMIN)
        self.assert_quota_usage("nc_user_count", 1)

        project.delete()
        self.assert_quota_usage("nc_user_count", 0)

    def test_customer_users_quota_decreases_when_projects_are_deleted_in_bulk(self):
        count = 2
        for _ in range(count):
            project = factories.ProjectFactory(customer=self.customer)
            user = factories.UserFactory()
            project.add_user(user, ProjectRole.ADMIN)

        self.assert_quota_usage("nc_user_count", count)

        for p in self.customer.projects.all():
            p.delete()

        self.assert_quota_usage("nc_user_count", 0)

    def assert_quota_usage(self, name, value):
        self.assertEqual(value, self.customer.get_quota_usage(name))


class UpdateCustomerUsersCountTest(test.APITestCase):
    """Test the bulk update_customer_users_count handler used by recalculate_quotas."""

    def test_updates_user_count_for_direct_customer_users(self):
        customer = factories.CustomerFactory()
        user = factories.UserFactory()
        customer.add_user(user, CustomerRole.OWNER)

        # Reset quota to test bulk recalculation
        QuotaUsage.objects.filter(scope=customer, name="nc_user_count").delete()
        self.assertEqual(customer.get_quota_usage("nc_user_count"), 0)

        # Trigger bulk recalculation
        update_customer_users_count(sender=None)

        self.assertEqual(customer.get_quota_usage("nc_user_count"), 1)

    def test_updates_user_count_for_project_users(self):
        customer = factories.CustomerFactory()
        project = factories.ProjectFactory(customer=customer)
        user = factories.UserFactory()
        project.add_user(user, ProjectRole.ADMIN)

        # Reset quota to test bulk recalculation
        QuotaUsage.objects.filter(scope=customer, name="nc_user_count").delete()
        self.assertEqual(customer.get_quota_usage("nc_user_count"), 0)

        # Trigger bulk recalculation
        update_customer_users_count(sender=None)

        self.assertEqual(customer.get_quota_usage("nc_user_count"), 1)

    def test_counts_unique_users_across_customer_and_project_roles(self):
        customer = factories.CustomerFactory()
        project = factories.ProjectFactory(customer=customer)
        user = factories.UserFactory()

        # Same user has both customer and project roles
        customer.add_user(user, CustomerRole.OWNER)
        project.add_user(user, ProjectRole.ADMIN)

        # Reset quota to test bulk recalculation
        QuotaUsage.objects.filter(scope=customer, name="nc_user_count").delete()

        # Trigger bulk recalculation
        update_customer_users_count(sender=None)

        # User should only be counted once
        self.assertEqual(customer.get_quota_usage("nc_user_count"), 1)

    def test_updates_multiple_customers_in_batch(self):
        customers = [factories.CustomerFactory() for _ in range(3)]
        for i, customer in enumerate(customers):
            for _ in range(i + 1):
                user = factories.UserFactory()
                customer.add_user(user, CustomerRole.OWNER)

        # Reset all quotas
        QuotaUsage.objects.filter(name="nc_user_count").delete()

        # Trigger bulk recalculation
        update_customer_users_count(sender=None)

        # Verify each customer has correct count
        for i, customer in enumerate(customers):
            self.assertEqual(
                customer.get_quota_usage("nc_user_count"),
                i + 1,
                f"Customer {i} should have {i + 1} users",
            )


@ddt
class CustomerUsersListTest(test.APITestCase):
    all_users = (
        "staff",
        "owner",
        "global_support",
        "customer_support",
    )

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.CustomerFactory.get_url(
            self.fixture.customer, action="users"
        )

    @data(*all_users)
    def test_user_can_list_customer_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        # call fixture to initiate all users:
        for user in self.all_users:
            getattr(self.fixture, user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        self.assertSetEqual(
            {user["role_name"] for user in response.data},
            {"CUSTOMER.OWNER", "CUSTOMER.SUPPORT"},
        )
        self.assertSetEqual(
            {user["uuid"] for user in response.data},
            {
                self.fixture.owner.uuid.hex,
                self.fixture.customer_support.uuid.hex,
            },
        )
        self.assertSetEqual(
            {
                user["projects"] and user["projects"][0]["role_name"] or None
                for user in response.data
            },
            {None},
        )

    def test_user_can_not_list_project_users(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_users_ordering_by_concatenated_name(self):
        walter = factories.UserFactory(full_name="", username="walter")
        admin = factories.UserFactory(full_name="admin", username="zzz")
        alice = factories.UserFactory(full_name="", username="alice")
        dave = factories.UserFactory(full_name="dave", username="dave")
        expected_order = [admin, alice, dave, walter]
        for user in expected_order:
            self.fixture.customer.add_user(user, CustomerRole.OWNER)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?o=concatenated_name")
        for serialized_user, expected_user in zip(response.data, expected_order):
            self.assertEqual(serialized_user["uuid"], expected_user.uuid.hex)

        # reversed order
        response = self.client.get(self.url + "?o=-concatenated_name")
        for serialized_user, expected_user in zip(response.data, expected_order[::-1]):
            self.assertEqual(serialized_user["uuid"], expected_user.uuid.hex)

    def test_filter_by_email(self):
        walter = factories.UserFactory(
            full_name="", username="walter", email="walter@gmail.com"
        )
        admin = factories.UserFactory(
            full_name="admin", username="zzz", email="admin@waldur.com"
        )
        alice = factories.UserFactory(
            full_name="", username="alice", email="alice@gmail.com"
        )

        for user in [admin, alice, walter]:
            self.fixture.customer.add_user(user, CustomerRole.OWNER)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url, {"email": "gmail.com"})
        self.assertEqual(len(response.data), 2)

    def test_filter_by_user_keyword(self):
        walter = factories.UserFactory(
            full_name="walter casey", username="walter", email="walter@gmail.com"
        )
        admin = factories.UserFactory(
            full_name="admin", username="zzz", email="admin@waldur.com"
        )
        alice = factories.UserFactory(
            full_name="alice keymer", username="alice", email="alice@gmail.com"
        )
        hans = factories.UserFactory(
            full_name="Hans Zimmer", username="hans", email="aliceandhans@gmail.com"
        )

        for user in [admin, alice, walter, hans]:
            self.fixture.customer.add_user(user, CustomerRole.OWNER)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url, {"user_keyword": "alice"})
        self.assertEqual(len(response.data), 2)

        response = self.client.get(self.url, {"user_keyword": "walter"})
        self.assertEqual(len(response.data), 1)

        response = self.client.get(self.url, {"user_keyword": "vettel"})
        self.assertEqual(len(response.data), 0)

    def test_filter_by_roles(self):
        walter = factories.UserFactory(
            full_name="", username="walter", email="walter@gmail.com"
        )
        admin = factories.UserFactory(
            full_name="admin", username="zzz", email="admin@waldur.com"
        )
        alice = factories.UserFactory(
            full_name="", username="alice", email="alice@gmail.com"
        )

        self.fixture.customer.add_user(walter, CustomerRole.SUPPORT)
        self.fixture.project.add_user(walter, ProjectRole.MANAGER)

        self.fixture.customer.add_user(admin, CustomerRole.OWNER)
        self.fixture.project.add_user(admin, ProjectRole.ADMIN)

        self.fixture.customer.add_user(alice, CustomerRole.SUPPORT)
        self.fixture.project.add_user(alice, ProjectRole.MEMBER)

        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 3)

        response = self.client.get(
            self.url,
            {
                "project_role": [
                    ProjectRole.ADMIN.name,
                    ProjectRole.MANAGER.name,
                ]
            },
        )
        usernames = [item["username"] for item in response.data]
        self.assertEqual(len(usernames), 2)
        self.assertTrue(admin.username in usernames)
        self.assertTrue(walter.username in usernames)

        response = self.client.get(
            self.url,
            {"organization_role": [CustomerRole.SUPPORT.name]},
        )
        usernames = [item["username"] for item in response.data]
        self.assertEqual(len(usernames), 2)
        self.assertTrue(walter.username in usernames)
        self.assertTrue(alice.username in usernames)

        response = self.client.get(
            self.url,
            {"organization_role": [CustomerRole.OWNER.name]},
        )
        usernames = [item["username"] for item in response.data]
        self.assertEqual(len(usernames), 1)
        self.assertTrue(admin.username in usernames)

        response = self.client.get(
            self.url,
            {
                "organization_role": [CustomerRole.OWNER.name],
                "project_role": [ProjectRole.MEMBER.name],
            },
        )
        usernames = [item["username"] for item in response.data]
        self.assertEqual(len(usernames), 2)
        self.assertTrue(admin.username in usernames)
        self.assertTrue(alice.username in usernames)

    def test_user_is_not_included_in_selection_if_he_has_required_role_in_different_organization(
        self,
    ):
        user = factories.UserFactory()
        self.fixture.customer.add_user(user, role=CustomerRole.OWNER)
        new_customer = factories.CustomerFactory()
        new_customer.add_user(user, role=ServiceProviderRole.MANAGER)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"organization_role": "service_manager"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_user_is_not_included_in_selection_if_he_has_required_role_in_project_of_different_organization(
        self,
    ):
        user = factories.UserFactory()
        self.fixture.customer.add_user(user, role=ProjectRole.ADMIN)
        new_project = factories.ProjectFactory()
        new_project.add_user(user, role=ProjectRole.MANAGER)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"project_role": ProjectRole.MANAGER.name})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_filter_by_role_if_permission_is_not_active(self):
        user = factories.UserFactory()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self.url, {"organization_role": ServiceProviderRole.MANAGER.name}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        self.fixture.customer.add_user(user, ServiceProviderRole.MANAGER)
        response = self.client.get(
            self.url, {"organization_role": ServiceProviderRole.MANAGER.name}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        # Even if user has project role, he is skipped when organization filter is applied
        self.fixture.project.add_user(user, ProjectRole.MEMBER)
        self.fixture.customer.remove_user(user)
        response = self.client.get(
            self.url, {"organization_role": ServiceProviderRole.MANAGER.name}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class AccountingIsRunningFilterTest(test.APITestCase):
    def setUp(self):
        self.enabled_customers = factories.CustomerFactory.create_batch(2)
        future_date = timezone.now() + timezone.timedelta(days=1)
        self.disabled_customers = factories.CustomerFactory.create_batch(
            3, accounting_start_date=future_date
        )
        self.all_customers = self.enabled_customers + self.disabled_customers

    def count_customers(self, accounting_is_running=None):
        self.client.force_authenticate(factories.UserFactory(is_staff=True))
        url = factories.CustomerFactory.get_list_url()
        params = {}
        if accounting_is_running in (True, False):
            params["accounting_is_running"] = accounting_is_running
        response = self.client.get(url, params)
        return len(response.data)

    @data(
        (True, "enabled_customers"),
        (False, "disabled_customers"),
        (None, "all_customers"),
    )
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_feature_is_enabled(self, params):
        actual = self.count_customers(params[0])
        expected = len(getattr(self, params[1]))
        self.assertEqual(expected, actual)

    @data(True, False, None)
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=False)
    def test_feature_is_disabled(self, param):
        actual = self.count_customers({"accounting_is_running": param})
        expected = len(self.all_customers)
        self.assertEqual(expected, actual)


class CustomerBlockedTest(CustomerBaseTest):
    def setUp(self):
        super().setUp()
        self.user = factories.UserFactory()
        self.staff = factories.UserFactory(is_staff=True)
        self.customer = factories.CustomerFactory(blocked=True)
        self.customer.add_user(self.user, CustomerRole.OWNER)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)

    def test_blocked_organization_is_not_available_for_updating(self):
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_url(customer=self.customer)
        response = self.client.put(url, {"name": "new_name"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blocked_organization_is_not_available_for_deleting(self):
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_url(customer=self.customer)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.staff)
        url = factories.CustomerFactory.get_url(customer=self.customer)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_creating_is_not_available_for_blocked_organization(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        self.client.force_authenticate(user=self.user)
        url = factories.ProjectFactory.get_list_url()
        data = {
            "name": "New project name",
            "customer": factories.CustomerFactory.get_url(self.customer),
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_deleting_is_not_available_for_blocked_organization(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_PROJECT)
        self.client.force_authenticate(user=self.user)
        project = factories.ProjectFactory(customer=self.customer)
        url = factories.ProjectFactory.get_url(project=project)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_updating_is_not_available_for_blocked_organization(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.client.force_authenticate(user=self.user)
        project = factories.ProjectFactory(customer=self.customer)
        url = factories.ProjectFactory.get_url(project=project)
        response = self.client.patch(url, {"name": "New project name"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_permission_adding_is_not_available_for_blocked_organization(self):
        user = factories.UserFactory()
        response = client_add_user(
            self.client, self.user, user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_permission_updating_is_not_available_for_blocked_organization(
        self,
    ):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.OWNER)

        response = client_update_user(
            self.client,
            self.user,
            user,
            self.customer,
            CustomerRole.OWNER,
            timezone.now() + datetime.timedelta(days=100),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_permission_deleting_is_not_available_for_blocked_organization(
        self,
    ):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.OWNER)
        response = client_delete_user(
            self.client, self.user, user, self.customer, CustomerRole.OWNER
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_permission_adding_is_not_available_for_blocked_organization(self):
        user = factories.UserFactory()
        project = factories.ProjectFactory(customer=self.customer)
        response = client_add_user(
            self.client, self.user, user, project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_permission_updating_is_not_available_for_blocked_organization(
        self,
    ):
        project = factories.ProjectFactory(customer=self.customer)
        user = factories.UserFactory()
        project.add_user(user, ProjectRole.ADMIN)
        response = client_update_user(
            self.client,
            self.user,
            user,
            project,
            ProjectRole.ADMIN,
            timezone.now() + datetime.timedelta(days=100),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_permission_deleting_is_not_available_for_blocked_organization(
        self,
    ):
        project = factories.ProjectFactory(customer=self.customer)
        user = factories.UserFactory()
        project.add_user(user, ProjectRole.ADMIN)
        response = client_delete_user(
            self.client,
            self.user,
            user,
            project,
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CustomerOrganizationGroupFilterTest(test.APITestCase):
    def setUp(self):
        self.organization_group = factories.OrganizationGroupFactory()
        self.customer1 = factories.CustomerFactory()
        self.customer2 = factories.CustomerFactory()
        self.customer2.organization_groups.add(self.organization_group)
        self.user = fixtures.UserFixture().staff
        self.url = factories.CustomerFactory.get_list_url()

    def test_filters(self):
        """Test of customers' list filter by organization_group name and organization_group UUID."""
        rows = [
            {
                "name": "organization_group_name",
                "valid": self.organization_group.name[2:],
                "invalid": "invalid",
            },
            {
                "name": "organization_group_uuid",
                "valid": self.organization_group.uuid.hex,
                "invalid": "invalid",
            },
        ]

        self.client.force_authenticate(self.user)

        for row in rows:
            response = self.client.get(self.url, data={row["name"]: row["valid"]})
            self.assertEqual(status.HTTP_200_OK, response.status_code)
            self.assertEqual(len(response.data), 1)

            response = self.client.get(self.url, data={row["name"]: row["invalid"]})
            if row["name"] == "organization_group_uuid":
                self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
            else:
                self.assertEqual(status.HTTP_200_OK, response.status_code)
                self.assertEqual(len(response.data), 0)


class CustomerInetFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.customer.save()

        # Explicitly portal-scoped: an entry only restricts sign-in when it says
        # it applies to the portal, so an unscoped one would leave the customer
        # visible and this test would assert nothing.
        self.access_subnet = AccessSubnet.objects.create(
            customer=self.customer, inet="128.0.0.0/16", applies_to_portal=True
        )

        # Patch only get_ip_address; normalize_ip_address must run for real so
        # the filter receives a canonical IP string (or None), matching production.
        self.patcher = mock.patch(
            "waldur_core.structure.managers.core_utils.get_ip_address"
        )
        self.mock = self.patcher.start()
        self.mock.return_value = "127.0.0.1"

        self.url = factories.CustomerFactory.get_list_url()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_staff_can_get_all_projects(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_user_can_get_project_only_if_his_ip_contains_inet(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

        self.customer = self.fixture.customer
        self.access_subnet.inet = "127.0.0.0/24"
        self.access_subnet.save()
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

        self.customer = self.fixture.customer
        self.access_subnet.inet = ""
        self.access_subnet.save()
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_filter_breaks_if_ip_address_is_not_defined(self):
        self.mock.return_value = None

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)


@freeze_time("2025-06-01")
class CustomerResourceQuotasTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        # Use fixed dates within the same year to ensure ANNUAL limit period tests work correctly
        self.current_date = datetime.datetime(2025, 10, 15, tzinfo=datetime.UTC)
        self.previous_month_date = datetime.datetime(2025, 8, 15, tzinfo=datetime.UTC)
        self.customer = self.fixture.customer
        self.empty_customer = factories.CustomerFactory()
        self.project1 = factories.ProjectFactory(customer=self.customer)
        self.project2 = factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory()
        self.component1 = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            name="CPU",
            measured_unit="vCPU",
            billing_type=BillingTypes.USAGE,
        )
        self.component2 = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            name="RAM",
            measured_unit="GB",
            billing_type=BillingTypes.USAGE,
        )
        self.limit_based_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="disk",
            name="Disk",
            measured_unit="GB",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.ANNUAL,
        )
        self.resource1 = marketplace_factories.ResourceFactory(
            project=self.project1,
            offering=self.offering,
            current_usages={"cpu": 2, "ram": 4},
            limits={"cpu": 8, "ram": 16},
        )
        self.resource2 = marketplace_factories.ResourceFactory(
            project=self.project2,
            offering=self.offering,
            current_usages={"cpu": 1, "ram": 2},
            limits={"cpu": 4, "ram": 8},
        )
        self.limit_based_resource = marketplace_factories.ResourceFactory(
            project=self.project1,
            offering=self.offering,
            limits={"disk": 100},
        )
        self.current_billing_period = datetime.date(2025, 10, 1)
        self.previous_billing_period = datetime.date(2025, 8, 1)
        self.limit_usage = marketplace_factories.ComponentUsageFactory(
            resource=self.limit_based_resource,
            component=self.limit_based_component,
            usage=10,
            date=self.current_date,
            billing_period=self.current_billing_period,
        )
        # create another limit_usage with 2 months back date
        marketplace_factories.ComponentUsageFactory(
            resource=self.limit_based_resource,
            component=self.limit_based_component,
            usage=15,
            date=self.previous_month_date,
            billing_period=self.previous_billing_period,
        )
        # create new usages for current month
        self.current_month_cpu_usage1 = marketplace_factories.ComponentUsageFactory(
            resource=self.resource1,
            component=self.component1,  # CPU component
            usage=5,
            date=self.current_date,
            billing_period=self.current_billing_period,
        )
        self.previous_month_cpu_usage1 = marketplace_factories.ComponentUsageFactory(
            resource=self.resource1,
            component=self.component1,  # CPU component
            usage=3,
            date=self.previous_month_date,
            billing_period=self.previous_billing_period,
        )
        self.current_month_cpu_usage2 = marketplace_factories.ComponentUsageFactory(
            resource=self.resource2,
            component=self.component1,  # CPU component
            usage=2,
            date=self.current_date,
            billing_period=self.current_billing_period,
        )

        self.current_month_ram_usage1 = marketplace_factories.ComponentUsageFactory(
            resource=self.resource1,
            component=self.component2,  # RAM component
            usage=10,
            date=self.current_date,
            billing_period=self.current_billing_period,
        )
        self.previous_month_ram_usage1 = marketplace_factories.ComponentUsageFactory(
            resource=self.resource1,
            component=self.component2,  # RAM component
            usage=8,
            date=self.previous_month_date,
            billing_period=self.previous_billing_period,
        )
        self.current_month_ram_usage2 = marketplace_factories.ComponentUsageFactory(
            resource=self.resource2,
            component=self.component2,
            usage=4,
            date=self.current_date,
            billing_period=self.current_billing_period,
        )
        self.url = factories.CustomerFactory.get_url(self.customer, "stats")

    def test_customer_with_no_resources(self):
        self.client.force_authenticate(self.fixture.staff)
        url = reverse("customer-stats", kwargs={"uuid": self.empty_customer.uuid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["components"], [])

    def test_customer_with_resources(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        components = response.data["components"]
        # Check component stats for CPU
        cpu_component = next(
            component for component in components if component["type"] == "cpu"
        )
        # current_usages is derived from the latest ComponentUsage per component:
        # resource1 cpu=5 (current month), resource2 cpu=2 → sum=7
        self.assertEqual(cpu_component["usage"], 7)
        self.assertEqual(cpu_component["limit"], 12)
        self.assertEqual(cpu_component["measured_unit"], "vCPU")
        # Check component stats for RAM
        ram_component = next(
            component for component in components if component["type"] == "ram"
        )
        # resource1 ram=10 (current month), resource2 ram=4 → sum=14
        self.assertEqual(ram_component["usage"], 14)
        self.assertEqual(ram_component["limit"], 24)
        self.assertEqual(ram_component["measured_unit"], "GB")

    @freeze_time("2025-10-15")
    def test_customer_with_resources_for_current_month(self):
        self.client.force_authenticate(self.fixture.staff)

        # Request with for_current_month=true
        url = self.url + "?for_current_month=true"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        components = response.data["components"]

        # Check component stats for CPU
        cpu_component = next(
            component for component in components if component["type"] == "cpu"
        )
        # Should only count the current month usage (5+2=7), not previous month or resource.current_usages
        self.assertEqual(
            cpu_component["usage"], 7
        )  # 5 from resource1 + 2 from resource2
        self.assertEqual(cpu_component["limit"], 12)
        self.assertEqual(cpu_component["measured_unit"], "vCPU")

        # Check component stats for RAM
        ram_component = next(
            component for component in components if component["type"] == "ram"
        )
        # Should only count the current month usage (10+4=14), not previous month or resource.current_usages
        self.assertEqual(
            ram_component["usage"], 14
        )  # 10 from resource1 + 4 from resource2
        self.assertEqual(ram_component["limit"], 24)
        self.assertEqual(ram_component["measured_unit"], "GB")

    @freeze_time("2025-10-15")
    def test_customer_with_limit_based_resources(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        components = response.data["components"]
        disk_component = next(
            component for component in components if component["type"] == "disk"
        )
        # disk is limit-based, so "usage" (for usage-based components) is 0
        self.assertEqual(disk_component["usage"], 0)
        self.assertEqual(disk_component["limit_usage"], 25)
        self.assertEqual(disk_component["measured_unit"], "GB")

    @freeze_time("2025-10-15")
    def test_customer_with_limit_based_resources_for_current_month(self):
        self.client.force_authenticate(self.fixture.staff)

        url = self.url + "?for_current_month=true"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        components = response.data["components"]
        disk_component = next(
            component for component in components if component["type"] == "disk"
        )
        self.assertEqual(disk_component["usage"], 0)
        self.assertEqual(disk_component["limit_usage"], 10)
        self.assertEqual(disk_component["measured_unit"], "GB")


class CustomerListHeadOptimizationTest(test.APITestCase):
    def test_head_query_count_does_not_depend_on_queryset_size(self):
        self.client.force_authenticate(user=factories.UserFactory(is_staff=True))

        # Warm-up caches
        self.client.head(factories.CustomerFactory.get_list_url())

        def count_queries():
            with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as queries:
                response = self.client.head(factories.CustomerFactory.get_list_url())
                return len(queries), int(dict(response.headers)[RESULT_COUNT_HEADER])

        factories.CustomerFactory.create_batch(3)
        first_pass_queries_count, first_pass_queryset_size = count_queries()

        factories.CustomerFactory.create_batch(3)
        second_pass_queries_count, second_pass_queryset_size = count_queries()

        self.assertGreater(first_pass_queries_count, 0)
        self.assertEqual(first_pass_queries_count, second_pass_queries_count)

        self.assertEqual(first_pass_queryset_size, 3)
        self.assertEqual(second_pass_queryset_size, 6)


class CustomerDefaultTaxPercentValidationTest(test.APITestCase):
    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.staff = factories.UserFactory(is_staff=True)

    def test_valid_tax_percent_decimal_values(self):
        """Test that valid Decimal values are accepted."""
        valid_values = [
            Decimal("0"),
            Decimal("0.00"),
            Decimal("10.50"),
            Decimal("25.75"),
            Decimal("100.00"),
            Decimal("200.00"),
        ]

        for value in valid_values:
            self.customer.default_tax_percent = value
            try:
                self.customer.full_clean()
            except ValidationError:
                self.fail(f"Valid value {value} should not raise ValidationError")

    def test_tax_percent_minimum_value_validation(self):
        """Test that values below 0 are rejected."""
        invalid_values = [
            Decimal("-0.01"),
            Decimal("-1.00"),
            Decimal("-10.50"),
        ]

        for value in invalid_values:
            self.customer.default_tax_percent = value
            with self.assertRaises(ValidationError) as context:
                self.customer.full_clean()
            self.assertIn("default_tax_percent", str(context.exception))

    def test_tax_percent_maximum_value_validation(self):
        """Test that values above 200 are rejected."""
        invalid_values = [
            Decimal("200.01"),
            Decimal("250.00"),
            Decimal("999.99"),
        ]

        for value in invalid_values:
            self.customer.default_tax_percent = value
            with self.assertRaises(ValidationError) as context:
                self.customer.full_clean()
            self.assertIn("default_tax_percent", str(context.exception))

    def test_tax_percent_boundary_values(self):
        """Test boundary values are handled correctly."""
        # Test exact boundary values
        boundary_values = [
            Decimal("0.00"),  # Minimum allowed
            Decimal("200.00"),  # Maximum allowed
        ]

        for value in boundary_values:
            self.customer.default_tax_percent = value
            try:
                self.customer.full_clean()
            except ValidationError:
                self.fail(f"Boundary value {value} should not raise ValidationError")

    def test_tax_percent_precision_validation(self):
        """Test that the field handles decimal precision correctly."""
        # Test with 2 decimal places (should work)
        self.customer.default_tax_percent = Decimal("15.99")
        try:
            self.customer.full_clean()
        except ValidationError:
            self.fail("Value with 2 decimal places should be valid")

    def test_tax_percent_api_validation(self):
        """Test validation through the API."""
        self.client.force_authenticate(user=self.staff)
        url = factories.CustomerFactory.get_url(self.customer)

        # Test valid value
        response = self.client.patch(url, {"default_tax_percent": "15.50"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Test invalid value (below minimum)
        response = self.client.patch(url, {"default_tax_percent": "-1.00"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("default_tax_percent", response.data)

        # Test invalid value (above maximum)
        response = self.client.patch(url, {"default_tax_percent": "250.00"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("default_tax_percent", response.data)

    def test_tax_percent_default_value(self):
        """Test that the default value is correctly set."""
        new_customer = factories.CustomerFactory()
        self.assertEqual(new_customer.default_tax_percent, Decimal("0"))

    def test_tax_percent_string_conversion(self):
        """Test that string values are properly converted to Decimal."""
        self.customer.default_tax_percent = "25.50"
        self.customer.save()
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.default_tax_percent, Decimal("25.50"))
        self.assertIsInstance(self.customer.default_tax_percent, Decimal)


class CustomerDescriptionTest(BaseCustomerMutationTest):
    """Test cases for the Customer description field functionality."""

    def test_customer_has_description_field(self):
        """Test that Customer model has description field from DescribableMixin."""
        customer = factories.CustomerFactory(description="Test description")
        self.assertEqual(customer.description, "Test description")

    def test_description_field_in_serializer(self):
        """Test that description field is exposed in the serializer."""
        customer = factories.CustomerFactory(
            description="Test organization description"
        )
        self.client.force_authenticate(user=self.fixture.staff)

        url = self._get_customer_url(customer)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("description", response.data)
        self.assertEqual(response.data["description"], "Test organization description")

    def test_create_customer_with_description(self):
        """Test creating a customer with description field."""
        self.client.force_authenticate(user=self.fixture.staff)

        payload = self._get_valid_payload()
        payload["description"] = "New customer with description"

        response = self.client.post(factories.CustomerFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["description"], "New customer with description")

        # Verify in database
        customer = Customer.objects.get(uuid=response.data["uuid"])
        self.assertEqual(customer.description, "New customer with description")

    def test_update_customer_description(self):
        """Test updating a customer's description field."""
        customer = factories.CustomerFactory(description="Original description")
        self.client.force_authenticate(user=self.fixture.staff)

        url = self._get_customer_url(customer)
        response = self.client.patch(url, {"description": "Updated description"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["description"], "Updated description")

        # Verify in database
        customer.refresh_from_db()
        self.assertEqual(customer.description, "Updated description")

    def test_description_field_ordering(self):
        """Test that customers can be ordered by description field."""
        # Create customers with distinct descriptions
        customer_a = factories.CustomerFactory(
            name="Customer A", description="Alpha description"
        )
        customer_b = factories.CustomerFactory(
            name="Customer B", description="Beta description"
        )
        customer_c = factories.CustomerFactory(
            name="Customer C", description="Charlie description"
        )

        self.client.force_authenticate(user=self.fixture.staff)

        # Test that ordering by description doesn't cause errors
        response = self.client.get(
            f"{factories.CustomerFactory.get_list_url()}?ordering=description"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify all customers have description field in response
        test_customer_uuids = {
            str(customer_a.uuid),
            str(customer_b.uuid),
            str(customer_c.uuid),
        }
        test_customers = [
            customer
            for customer in response.data
            if customer["uuid"] in test_customer_uuids
        ]

        # Ensure all our test customers are present with description field
        self.assertEqual(len(test_customers), 3)
        for customer in test_customers:
            self.assertIn("description", customer)
            self.assertIn(
                customer["description"],
                ["Alpha description", "Beta description", "Charlie description"],
            )

        # Test descending order also works without error
        response = self.client.get(
            f"{factories.CustomerFactory.get_list_url()}?ordering=-description"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_description_field_blank_allowed(self):
        """Test that description field can be blank."""
        customer = factories.CustomerFactory(description="")
        self.assertEqual(customer.description, "")

        self.client.force_authenticate(user=self.fixture.staff)

        # Test creating customer with empty description
        payload = self._get_valid_payload()
        payload["description"] = ""

        response = self.client.post(factories.CustomerFactory.get_list_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["description"], "")

    def test_description_max_length(self):
        """Test that description field respects maximum length from DescribableMixin."""
        from waldur_core.core.models import DESCRIPTION_LENGTH

        # Test within limit
        valid_description = "A" * DESCRIPTION_LENGTH
        customer = factories.CustomerFactory(description=valid_description)
        customer.full_clean()  # Should not raise

        # Test exceeding limit
        invalid_description = "A" * (DESCRIPTION_LENGTH + 1)
        customer = factories.CustomerFactory.build(description=invalid_description)

        with self.assertRaises(ValidationError):
            customer.full_clean()


class CustomerAddressFieldsTest(BaseCustomerMutationTest):
    """Test cases for the Customer address fields functionality."""

    def test_address_fields_are_visible_in_get_response(self):
        """Test that all address fields are visible in GET response."""
        customer = factories.CustomerFactory(
            city="Tallinn",
            state="Harjumaa",
            parish="Kesklinn",
            street="Vabaduse väljak",
            house_nr="10",
            apartment_nr="5A",
            household="Building A",
        )
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.get(self._get_customer_url(customer))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("city", response.data)
        self.assertIn("state", response.data)
        self.assertIn("parish", response.data)
        self.assertIn("street", response.data)
        self.assertIn("house_nr", response.data)
        self.assertIn("apartment_nr", response.data)
        self.assertIn("household", response.data)
        self.assertEqual(response.data["city"], "Tallinn")
        self.assertEqual(response.data["state"], "Harjumaa")
        self.assertEqual(response.data["parish"], "Kesklinn")
        self.assertEqual(response.data["street"], "Vabaduse väljak")
        self.assertEqual(response.data["house_nr"], "10")
        self.assertEqual(response.data["apartment_nr"], "5A")
        self.assertEqual(response.data["household"], "Building A")

    def test_address_fields_are_updatable(self):
        """Test that staff can update address fields."""
        customer = factories.CustomerFactory()
        self.client.force_authenticate(user=self.fixture.staff)

        payload = {
            "city": "Tartu",
            "state": "Tartumaa",
            "parish": "Vanemuine",
            "street": "Rüütli",
            "house_nr": "23",
            "apartment_nr": "1B",
            "household": "Building C",
        }

        response = self.client.patch(self._get_customer_url(customer), payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["city"], "Tartu")
        self.assertEqual(response.data["state"], "Tartumaa")
        self.assertEqual(response.data["parish"], "Vanemuine")
        self.assertEqual(response.data["street"], "Rüütli")
        self.assertEqual(response.data["house_nr"], "23")
        self.assertEqual(response.data["apartment_nr"], "1B")
        self.assertEqual(response.data["household"], "Building C")

        # Verify in database
        customer.refresh_from_db()
        self.assertEqual(customer.city, "Tartu")
        self.assertEqual(customer.state, "Tartumaa")

    def test_address_fields_can_be_blank(self):
        """Test that address fields can be blank."""
        customer = factories.CustomerFactory(
            city="",
            state="",
            parish="",
            street="",
            house_nr="",
            apartment_nr="",
            household="",
        )
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.get(self._get_customer_url(customer))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["city"], "")
        self.assertEqual(response.data["state"], "")
        self.assertEqual(response.data["parish"], "")
        self.assertEqual(response.data["street"], "")
        self.assertEqual(response.data["house_nr"], "")
        self.assertEqual(response.data["apartment_nr"], "")
        self.assertEqual(response.data["household"], "")

    def test_update_individual_address_field(self):
        """Test updating a single address field at a time."""
        customer = factories.CustomerFactory(city="", state="")
        self.client.force_authenticate(user=self.fixture.staff)

        # Update only city
        response = self.client.patch(
            self._get_customer_url(customer), {"city": "Tallinn"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["city"], "Tallinn")
        self.assertEqual(response.data["state"], "")

        # Update only state
        response = self.client.patch(
            self._get_customer_url(customer), {"state": "Harjumaa"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["city"], "Tallinn")
        self.assertEqual(response.data["state"], "Harjumaa")

    def test_create_customer_with_address_fields(self):
        """Test creating a customer with address fields."""
        self.client.force_authenticate(user=self.fixture.staff)

        payload = self._get_valid_payload()
        payload.update(
            {
                "city": "Pärnu",
                "state": "Pärnumaa",
                "street": "Karja",
                "house_nr": "14",
            }
        )

        response = self.client.post(factories.CustomerFactory.get_list_url(), payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["city"], "Pärnu")
        self.assertEqual(response.data["state"], "Pärnumaa")
        self.assertEqual(response.data["street"], "Karja")
        self.assertEqual(response.data["house_nr"], "14")

        # Verify in database
        customer = Customer.objects.get(uuid=response.data["uuid"])
        self.assertEqual(customer.city, "Pärnu")
        self.assertEqual(customer.state, "Pärnumaa")

    def test_address_fields_max_length(self):
        """Test that address fields respect their maximum length."""
        customer = factories.CustomerFactory()
        self.client.force_authenticate(user=self.fixture.staff)

        # city, state, parish, house_nr, apartment_nr are max_length=100
        # street is max_length=200, household is max_length=100
        test_cases = [
            {"city": "A" * 100},  # Valid
            {"state": "B" * 100},  # Valid
            {"parish": "C" * 100},  # Valid
            {"street": "D" * 200},  # Valid
            {"house_nr": "E" * 100},  # Valid
            {"apartment_nr": "F" * 100},  # Valid
            {"household": "G" * 100},  # Valid
        ]

        for payload in test_cases:
            response = self.client.patch(self._get_customer_url(customer), payload)
            self.assertEqual(
                response.status_code,
                status.HTTP_200_OK,
                f"Failed to update with payload: {payload}",
            )

    def test_owner_can_update_address_fields_with_permission(self):
        """Test that owner can update address fields with UPDATE_CUSTOMER permission."""
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_CUSTOMER)
        self.client.force_authenticate(user=self.fixture.owner)

        response = self.client.patch(
            self._get_customer_url(self.fixture.customer),
            {"city": "Tallinn", "state": "Harjumaa"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["city"], "Tallinn")
        self.assertEqual(response.data["state"], "Harjumaa")
