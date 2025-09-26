from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.permissions import enums as permission_enums
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace.models import OfferingStates
from waldur_mastermind.marketplace.tests import factories


@ddt
class OfferingComplianceChecklistUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer, shared=True, state=OfferingStates.DRAFT
        )

        # Add required permissions
        CustomerRole.OWNER.add_permission(
            permission_enums.PermissionEnum.UPDATE_OFFERING_OPTIONS
        )

        # Create test checklists
        self.checklist1 = checklist_factories.ChecklistFactory(
            name="Compliance Checklist 1",
            checklist_type=ChecklistTypes.OFFERING_COMPLIANCE,
        )
        self.checklist2 = checklist_factories.ChecklistFactory(
            name="Compliance Checklist 2",
            checklist_type=ChecklistTypes.OFFERING_COMPLIANCE,
        )

    @data("staff", "owner")
    def test_update_compliance_checklist(self, user):
        """Test updating compliance checklist with valid checklist UUID."""
        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(
            url, {"compliance_checklist": self.checklist1.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.compliance_checklist, self.checklist1)

    @data("staff", "owner")
    def test_update_compliance_checklist_to_different_checklist(self, user):
        """Test changing compliance checklist to a different one."""
        # First set a checklist
        self.offering.compliance_checklist = self.checklist1
        self.offering.save()

        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(
            url, {"compliance_checklist": self.checklist2.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.compliance_checklist, self.checklist2)

    @data("staff", "owner")
    def test_remove_compliance_checklist(self, user):
        """Test removing compliance checklist by setting it to null."""
        # First set a checklist
        self.offering.compliance_checklist = self.checklist1
        self.offering.save()

        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(url, {"compliance_checklist": None})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertIsNone(self.offering.compliance_checklist)

    def test_unauthorized_user_cannot_update_compliance_checklist(self):
        """Test that unrelated users cannot update compliance checklist."""
        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(self.fixture.user)

        response = self.client.post(
            url, {"compliance_checklist": self.checklist1.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_checklist_uuid_returns_400(self):
        """Test that invalid checklist UUID returns validation error."""
        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(url, {"compliance_checklist": "invalid-uuid"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nonexistent_checklist_uuid_returns_400(self):
        """Test that non-existent checklist UUID returns validation error."""
        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(self.fixture.staff)

        # Use a valid UUID format but non-existent checklist
        from uuid import uuid4

        fake_uuid = uuid4()

        response = self.client.post(url, {"compliance_checklist": fake_uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_request_body_succeeds_without_changes(self):
        """Test that empty request body succeeds without making changes."""
        # Set initial compliance checklist
        self.offering.compliance_checklist = self.checklist1
        self.offering.save()

        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        # Should remain unchanged
        self.assertEqual(self.offering.compliance_checklist, self.checklist1)

    @data("admin", "manager")
    def test_project_users_cannot_update_compliance_checklist(self, user):
        """Test that project-level users cannot update compliance checklist."""
        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(
            url, {"compliance_checklist": self.checklist1.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_service_provider_can_update_compliance_checklist(self):
        """Test that service provider can update compliance checklist."""
        # Create a service provider user
        service_provider_user = UserFactory()
        self.offering.customer.add_user(service_provider_user, CustomerRole.OWNER)

        url = factories.OfferingFactory.get_url(
            self.offering, "update_compliance_checklist"
        )
        self.client.force_authenticate(service_provider_user)

        response = self.client.post(
            url, {"compliance_checklist": self.checklist1.uuid.hex}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.compliance_checklist, self.checklist1)
