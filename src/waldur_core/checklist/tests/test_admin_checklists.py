from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import models
from waldur_core.checklist.tests import factories, fixtures
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import enums


@ddt
class ChecklistAdminGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.ChecklistFactory.get_admin_list_url()

    @data("staff")
    def test_user_can_list_checklists(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("owner")
    def test_user_cannot_list_checklists(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ChecklistAdminCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.url = factories.ChecklistFactory.get_admin_list_url()

    def _get_payload(self):
        return {
            "name": "my_checklist",
            "checklist_type": enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
        }

    @data("staff")
    def test_user_can_create_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Checklist.objects.filter(name="my_checklist").exists())

    @data("owner")
    def test_user_cannot_create_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ChecklistAdminUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.ChecklistFactory.get_admin_url(self.fixture.checklist)

    def _get_payload(self):
        return {
            "name": "new_checklist",
        }

    @data("staff")
    def test_user_can_update_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(models.Checklist.objects.filter(name="new_checklist").exists())

    @data("owner")
    def test_user_cannot_update_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.Checklist.objects.filter(name="new_checklist").exists())


@ddt
class ChecklistAdminDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.ChecklistFactory.get_admin_url(self.fixture.checklist)

    @data("staff")
    def test_user_can_delete_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Checklist.objects.filter(name="my_checklist").exists())

    @data("owner")
    def test_user_cannot_delete_checklist(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Checklist.objects.filter(name="my_checklist").exists())


@ddt
class ChecklistFilterTest(test.APITransactionTestCase):
    """Test ChecklistFilter functionality for filtering checklists by type."""

    def setUp(self):
        self.fixture = fixtures.CheckListFixture()

        # Create checklists of different types
        self.project_checklist = factories.ChecklistFactory(
            name="Project Compliance Checklist",
            checklist_type=enums.ChecklistTypes.PROJECT_COMPLIANCE,
        )
        self.proposal_checklist = factories.ChecklistFactory(
            name="Proposal Compliance Checklist",
            checklist_type=enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
        )
        self.offering_checklist = factories.ChecklistFactory(
            name="Offering Compliance Checklist",
            checklist_type=enums.ChecklistTypes.OFFERING_COMPLIANCE,
        )
        self.metadata_checklist = factories.ChecklistFactory(
            name="Project Metadata Checklist",
            checklist_type=enums.ChecklistTypes.PROJECT_METADATA,
        )

        self.url = factories.ChecklistFactory.get_admin_list_url()

    @data("staff")
    def test_filter_by_single_checklist_type(self, user):
        """Test filtering checklists by a single checklist type."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Filter by project_compliance type
        response = self.client.get(
            self.url, {"checklist_type": enums.ChecklistTypes.PROJECT_COMPLIANCE}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()

        # Should return only the project compliance checklists
        project_checklists = [
            item
            for item in response_data
            if item["checklist_type"] == enums.ChecklistTypes.PROJECT_COMPLIANCE
        ]
        self.assertGreaterEqual(len(project_checklists), 1)
        # Verify our specific checklist is included
        checklist_names = {item["name"] for item in project_checklists}
        self.assertIn(self.project_checklist.name, checklist_names)

        # Should not include other types
        other_types = [
            item
            for item in response_data
            if item["checklist_type"] != enums.ChecklistTypes.PROJECT_COMPLIANCE
        ]
        self.assertEqual(len(other_types), 0)

    @data("staff")
    def test_filter_by_multiple_checklist_types(self, user):
        """Test filtering checklists by multiple checklist types using checklist_type__in."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Filter by both project_compliance and offering_compliance types
        response = self.client.get(
            self.url,
            {
                "checklist_type__in": [
                    enums.ChecklistTypes.PROJECT_COMPLIANCE,
                    enums.ChecklistTypes.OFFERING_COMPLIANCE,
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()

        # Should return project and offering compliance checklists
        filtered_types = {item["checklist_type"] for item in response_data}
        expected_types = {
            enums.ChecklistTypes.PROJECT_COMPLIANCE,
            enums.ChecklistTypes.OFFERING_COMPLIANCE,
        }
        self.assertEqual(filtered_types, expected_types)

        # Should have at least 2 results (our 2 created checklists, possibly more from fixtures)
        self.assertGreaterEqual(len(response_data), 2)

        # Verify our specific checklists are present
        checklist_names = {item["name"] for item in response_data}
        self.assertIn(self.project_checklist.name, checklist_names)
        self.assertIn(self.offering_checklist.name, checklist_names)

    @data("staff")
    def test_filter_by_offering_compliance_type(self, user):
        """Test filtering specifically for offering compliance checklists."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        response = self.client.get(
            self.url, {"checklist_type": enums.ChecklistTypes.OFFERING_COMPLIANCE}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()

        # Should return only offering compliance checklists
        self.assertEqual(len(response_data), 1)
        self.assertEqual(
            response_data[0]["checklist_type"], enums.ChecklistTypes.OFFERING_COMPLIANCE
        )
        self.assertEqual(response_data[0]["name"], self.offering_checklist.name)

    @data("staff")
    def test_filter_by_nonexistent_checklist_type(self, user):
        """Test filtering by a non-existent checklist type returns empty results."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        response = self.client.get(self.url, {"checklist_type": "nonexistent_type"})

        # Should return 400 for invalid choice value
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("staff")
    def test_no_filter_returns_all_checklists(self, user):
        """Test that without filters, all checklists are returned."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()

        # Should return all created checklists plus the one from fixture
        self.assertGreaterEqual(len(response_data), 5)  # 4 created + 1 from fixture

        # Verify all types are present
        checklist_types = {item["checklist_type"] for item in response_data}
        expected_types = {
            enums.ChecklistTypes.PROJECT_COMPLIANCE,
            enums.ChecklistTypes.PROPOSAL_COMPLIANCE,
            enums.ChecklistTypes.OFFERING_COMPLIANCE,
            enums.ChecklistTypes.PROJECT_METADATA,
        }
        self.assertTrue(expected_types.issubset(checklist_types))

    @data("staff")
    def test_filter_combination_with_other_fields(self, user):
        """Test that checklist_type filter works in combination with other query parameters."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        # Test filtering by type and searching by name
        response = self.client.get(
            self.url,
            {
                "checklist_type": enums.ChecklistTypes.PROJECT_COMPLIANCE,
                "search": "Project",  # Assuming search is supported
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()

        # All results should be project compliance type
        for item in response_data:
            self.assertEqual(
                item["checklist_type"], enums.ChecklistTypes.PROJECT_COMPLIANCE
            )
