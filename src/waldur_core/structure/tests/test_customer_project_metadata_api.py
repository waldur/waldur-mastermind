"""Tests for Customer project_metadata_checklist field API functionality."""

from rest_framework import status, test

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class CustomerProjectMetadataAPITest(test.APITransactionTestCase):
    """Test Customer project_metadata_checklist field API functionality."""

    def setUp(self):
        """Set up test data."""
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.staff = self.fixture.staff
        self.owner = self.fixture.owner

        self.customer_url = structure_factories.CustomerFactory.get_url(self.customer)

        # Create a PROJECT_METADATA checklist
        self.checklist = checklist_factories.ChecklistFactory(
            name="Project Metadata Checklist",
            checklist_type=ChecklistTypes.PROJECT_METADATA,
        )

    def test_staff_can_assign_project_metadata_checklist(self):
        """Test that staff users can assign a project metadata checklist to a customer."""
        self.client.force_authenticate(user=self.staff)

        response = self.client.patch(
            self.customer_url,
            {"project_metadata_checklist": str(self.checklist.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.project_metadata_checklist, self.checklist)

    def test_staff_can_unset_project_metadata_checklist(self):
        """Test that staff users can unset a project metadata checklist."""
        # Set up customer with a checklist
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self.customer_url,
            {"project_metadata_checklist": None},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.project_metadata_checklist)

    def test_non_staff_cannot_assign_project_metadata_checklist(self):
        """Test that non-staff users cannot modify customer (need UPDATE_CUSTOMER permission)."""
        self.client.force_authenticate(user=self.owner)

        response = self.client.patch(
            self.customer_url,
            {"project_metadata_checklist": str(self.checklist.uuid)},
        )

        # Non-staff users don't have UPDATE_CUSTOMER permission, so they get 403
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.project_metadata_checklist)

    def test_invalid_checklist_type_rejected(self):
        """Test that non-PROJECT_METADATA checklists are rejected."""
        # Create a different type of checklist
        wrong_checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROPOSAL_COMPLIANCE
        )

        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self.customer_url,
            {"project_metadata_checklist": str(wrong_checklist.uuid)},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_metadata_checklist", response.data)

    def test_nonexistent_checklist_rejected(self):
        """Test that invalid checklist UUIDs are rejected."""
        import uuid

        fake_uuid = str(uuid.uuid4())

        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self.customer_url,
            {"project_metadata_checklist": fake_uuid},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_metadata_checklist", response.data)

    def test_project_metadata_checklist_field_in_serializer_output(self):
        """Test that project_metadata_checklist field is included in serializer output."""
        # Assign a checklist
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.customer_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("project_metadata_checklist", response.data)
        # The UUID is returned as StringUUID, so convert both to string for comparison
        self.assertEqual(
            str(response.data["project_metadata_checklist"]), str(self.checklist.uuid)
        )

    def test_project_metadata_checklist_field_null_in_output(self):
        """Test that project_metadata_checklist field shows null when not set."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.customer_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("project_metadata_checklist", response.data)
        self.assertIsNone(response.data["project_metadata_checklist"])

    def test_field_is_staff_only(self):
        """Test that project_metadata_checklist field is staff-only."""
        # Set a checklist on the customer
        self.customer.project_metadata_checklist = self.checklist
        self.customer.save()

        # Non-staff user should see the field but as read-only
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self.customer_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("project_metadata_checklist", response.data)
        # Should show the actual value since they can read it
        self.assertEqual(
            str(response.data["project_metadata_checklist"]), str(self.checklist.uuid)
        )
