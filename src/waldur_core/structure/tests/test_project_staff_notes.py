from unittest import mock

from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class ProjectStaffNotesTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff_user = factories.UserFactory(is_staff=True)
        self.support_user = factories.UserFactory(is_support=True)
        self.regular_user = factories.UserFactory()

        # Add required permissions for updating projects
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROJECT)

        # Add some initial staff notes
        self.project.staff_notes = "Initial staff notes for testing"
        self.project.save()

        self.url = reverse("project-detail", kwargs={"uuid": self.project.uuid})

    def test_staff_user_can_view_staff_notes(self):
        """Staff users should be able to see the staff_notes field."""
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("staff_notes", response.data)
        self.assertEqual(
            response.data["staff_notes"], "Initial staff notes for testing"
        )

    def test_staff_user_can_edit_staff_notes(self):
        """Staff users should be able to edit the staff_notes field."""
        self.client.force_authenticate(user=self.staff_user)

        new_notes = "Updated staff notes by staff user"
        response = self.client.patch(self.url, {"staff_notes": new_notes})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["staff_notes"], new_notes)

        # Verify the change was persisted
        self.project.refresh_from_db()
        self.assertEqual(self.project.staff_notes, new_notes)

    def test_support_user_can_view_staff_notes(self):
        """Support users should be able to see the staff_notes field."""
        self.client.force_authenticate(user=self.support_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("staff_notes", response.data)
        self.assertEqual(
            response.data["staff_notes"], "Initial staff notes for testing"
        )

    def test_support_user_cannot_edit_staff_notes(self):
        """Support users should not be able to edit the staff_notes field."""
        # Use a customer owner who can update projects but isn't staff
        customer_owner = self.fixture.owner
        customer_owner.is_support = True
        customer_owner.save()

        self.client.force_authenticate(user=customer_owner)

        original_notes = self.project.staff_notes
        new_notes = "Attempted update by support user"
        response = self.client.patch(
            self.url, {"staff_notes": new_notes, "name": "Updated name"}
        )

        # The request should succeed (user can update projects)
        # but the staff_notes field should remain unchanged due to read-only
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["staff_notes"], original_notes)

        # Verify the field was not changed in the database
        self.project.refresh_from_db()
        self.assertEqual(self.project.staff_notes, original_notes)

    def test_regular_user_cannot_see_staff_notes(self):
        """Regular users should not see the staff_notes field at all."""
        # Add user to project so they can access it
        self.project.add_user(self.regular_user, ProjectRole.ADMIN)

        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("staff_notes", response.data)

    def test_regular_user_cannot_edit_staff_notes(self):
        """Regular users should not be able to edit staff_notes field."""
        # Use customer owner who can update projects but isn't staff/support
        customer_owner = self.fixture.owner

        self.client.force_authenticate(user=customer_owner)

        original_notes = self.project.staff_notes
        response = self.client.patch(
            self.url, {"staff_notes": "Attempted hack", "name": "Updated name"}
        )

        # Request should succeed but staff_notes should be unchanged
        # (field is hidden from regular users so the update is ignored)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the field was not changed
        self.project.refresh_from_db()
        self.assertEqual(self.project.staff_notes, original_notes)

    def test_staff_notes_field_accepts_html_content(self):
        """Staff notes should accept and clean HTML content like description field."""
        self.client.force_authenticate(user=self.staff_user)

        html_content = (
            "<p>Rich text <strong>notes</strong> with <em>formatting</em></p>"
        )
        response = self.client.patch(self.url, {"staff_notes": html_content})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # HTMLCleanField should preserve safe HTML
        self.assertIn("notes", response.data["staff_notes"])

    def test_staff_notes_field_accepts_long_content(self):
        """Staff notes should accept content up to DESCRIPTION_LENGTH (4096 chars)."""
        self.client.force_authenticate(user=self.staff_user)

        # Test with content close to the limit
        long_content = "A" * 4000  # Well within 4096 limit
        response = self.client.patch(self.url, {"staff_notes": long_content})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["staff_notes"], long_content)

    def test_staff_notes_empty_by_default(self):
        """New projects should have empty staff_notes by default."""
        new_project = factories.ProjectFactory()
        self.client.force_authenticate(user=self.staff_user)

        url = reverse("project-detail", kwargs={"uuid": new_project.uuid})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["staff_notes"], "")


class ProjectStaffNotesSchemaTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_openapi_schema_includes_staff_notes_field(self):
        """OpenAPI schema generation should include staff_notes field."""
        # Create a mock view with swagger_fake_view attribute (schema generation)
        mock_view = mock.Mock()
        mock_view.swagger_fake_view = True

        # Create a mock request
        mock_request = mock.Mock()
        mock_request.user = self.fixture.staff

        context = {
            "request": mock_request,
            "view": mock_view,
        }

        # Import and test the serializer
        from waldur_core.structure.serializers import ProjectSerializer

        serializer = ProjectSerializer(context=context)
        fields = serializer.get_fields()

        # staff_notes should be included in schema generation
        self.assertIn("staff_notes", fields)


class ProjectStaffNotesListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.staff_user = factories.UserFactory(is_staff=True)
        self.support_user = factories.UserFactory(is_support=True)
        self.regular_user = factories.UserFactory()

        # Create projects with staff notes
        self.project1 = factories.ProjectFactory(staff_notes="Notes for project 1")
        self.project2 = factories.ProjectFactory(staff_notes="Notes for project 2")

        self.url = reverse("project-list")

    def test_staff_user_sees_staff_notes_in_list(self):
        """Staff users should see staff_notes in project list."""
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Handle both paginated and non-paginated responses
        if isinstance(response.data, dict) and "results" in response.data:
            projects = response.data["results"]
        else:
            projects = response.data

        found_projects = {
            str(self.project1.uuid): False,
            str(self.project2.uuid): False,
        }
        for project in projects:
            if project["uuid"] == str(self.project1.uuid):
                self.assertEqual(project["staff_notes"], "Notes for project 1")
                found_projects[str(self.project1.uuid)] = True
            elif project["uuid"] == str(self.project2.uuid):
                self.assertEqual(project["staff_notes"], "Notes for project 2")
                found_projects[str(self.project2.uuid)] = True

    def test_support_user_sees_staff_notes_in_list(self):
        """Support users should see staff_notes in project list."""
        self.client.force_authenticate(user=self.support_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Handle both paginated and non-paginated responses
        if isinstance(response.data, dict) and "results" in response.data:
            projects = response.data["results"]
        else:
            projects = response.data

        # Find our test project and verify staff_notes is present
        for project in projects:
            if project["uuid"] == str(self.project1.uuid):
                self.assertIn("staff_notes", project)
                self.assertEqual(project["staff_notes"], "Notes for project 1")

        # Ensure we found at least one project (staff can see all)
        self.assertTrue(len(projects) > 0)

    def test_regular_user_does_not_see_staff_notes_in_list(self):
        """Regular users should not see staff_notes in project list."""
        # Add user to projects so they can see them
        self.project1.add_user(self.regular_user, ProjectRole.ADMIN)
        self.project2.add_user(self.regular_user, ProjectRole.ADMIN)

        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Handle both paginated and non-paginated responses
        if isinstance(response.data, dict) and "results" in response.data:
            projects = response.data["results"]
        else:
            projects = response.data

        # Check that staff_notes is not present in any project
        for project in projects:
            self.assertNotIn("staff_notes", project)
