"""
Test for auto project creation functionality in group invitations
"""

from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.models import GroupInvitation, PermissionRequest
from waldur_core.users.serializers import SubmitRequestSerializer


class AutoProjectCreationTest(TestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory()

        # Create roles
        self.project_admin_role, _ = Role.objects.get_or_create(
            name="PROJECT.ADMIN",
            content_type=ContentType.objects.get_for_model(Project),
        )

        self.customer_owner_role, _ = Role.objects.get_or_create(
            name="CUSTOMER.OWNER",
            content_type=ContentType.objects.get_for_model(Customer),
        )

    def test_traditional_group_invitation_unchanged(self):
        """Test that traditional group invitations still work as before"""
        # Get customer owner role

        # Create traditional group invitation
        invitation = GroupInvitation.objects.create(
            customer=self.customer,
            role=self.customer_owner_role,
            scope=self.customer,
            created_by=self.staff,
            auto_create_project=False,
        )

        # Create permission request
        request = PermissionRequest.objects.create(
            invitation=invitation, created_by=self.user
        )

        # Approve the request
        request.approve(self.staff)

        # Check that user got customer permissions
        self.assertTrue(
            self.user.userrole_set.filter(
                role=self.customer_owner_role, scope=self.customer
            ).exists()
        )

        # Check that no project was created
        self.assertEqual(Project.objects.filter(customer=self.customer).count(), 0)

    def test_auto_project_creation_workflow(self):
        """Test that auto project creation works correctly"""
        # Create group invitation with auto project creation
        invitation = GroupInvitation.objects.create(
            customer=self.customer,
            role=self.project_admin_role,
            scope=self.customer,
            created_by=self.staff,
            auto_create_project=True,
            project_name_template="{username}_test_project",
        )

        # Create permission request
        request = PermissionRequest.objects.create(
            invitation=invitation, created_by=self.user
        )

        # Approve the request
        request.approve(self.staff)

        # Check that project was created
        expected_project_name = f"{self.user.username}_test_project"
        project = Project.objects.get(
            name=expected_project_name, customer=self.customer
        )

        # Check that user got project permissions
        self.assertTrue(
            self.user.userrole_set.filter(
                role=self.project_admin_role, scope=project
            ).exists()
        )

        # Check that user did NOT get customer permissions
        self.assertFalse(self.user.userrole_set.filter(scope=self.customer).exists())

    def test_duplicate_project_prevention(self):
        """Test that multiple invitations for same user don't create duplicate projects"""
        # Create first group invitation
        invitation1 = GroupInvitation.objects.create(
            customer=self.customer,
            role=self.project_admin_role,
            scope=self.customer,
            created_by=self.staff,
            auto_create_project=True,
            project_name_template="{username}_lab",
        )

        # Create second group invitation (same template)
        invitation2 = GroupInvitation.objects.create(
            customer=self.customer,
            role=self.project_admin_role,
            scope=self.customer,
            created_by=self.staff,
            auto_create_project=True,
            project_name_template="{username}_lab",
        )

        # Create and approve first request
        request1 = PermissionRequest.objects.create(
            invitation=invitation1, created_by=self.user
        )
        request1.approve(self.staff)

        # Create and approve second request
        request2 = PermissionRequest.objects.create(
            invitation=invitation2, created_by=self.user
        )
        request2.approve(self.staff)

        # Check that only one project was created
        expected_project_name = f"{self.user.username}_lab"
        projects = Project.objects.filter(
            name=expected_project_name, customer=self.customer
        )
        self.assertEqual(projects.count(), 1)

        # Check that user has project permissions (potentially multiple roles)
        project = projects.first()
        user_roles = self.user.userrole_set.filter(scope=project)
        self.assertTrue(user_roles.exists())

    def test_project_name_resolution(self):
        """Test that project name template variables are resolved correctly"""
        self.user.first_name = "John"
        self.user.last_name = "Doe"
        self.user.save()

        invitation = GroupInvitation.objects.create(
            customer=self.customer,
            role=self.project_admin_role,
            scope=self.customer,
            created_by=self.staff,
            auto_create_project=True,
            project_name_template="{full_name}_research",
        )

        request = PermissionRequest.objects.create(
            invitation=invitation, created_by=self.user
        )
        request.approve(self.staff)

        # Check project name
        expected_name = f"{self.user.get_full_name()}_research"
        self.assertTrue(
            Project.objects.filter(name=expected_name, customer=self.customer).exists()
        )


class SubmitRequestProjectNameValidationTest(TestCase):
    """The custom project name provided at invitation acceptance must honour
    the same configurable pattern as the main project API, so it cannot be used
    to bypass the limit."""

    @override_config(PROJECT_NAME_REGEX=r"^.{1,32}$")
    def test_custom_project_name_exceeding_pattern_is_rejected(self):
        serializer = SubmitRequestSerializer(data={"project_name": "x" * 33})
        self.assertFalse(serializer.is_valid())
        self.assertIn("project_name", serializer.errors)

    @override_config(PROJECT_NAME_REGEX=r"^.{1,32}$")
    def test_custom_project_name_matching_pattern_is_accepted(self):
        serializer = SubmitRequestSerializer(data={"project_name": "x" * 32})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    @override_config(PROJECT_NAME_REGEX=r"^.{1,32}$")
    def test_blank_custom_project_name_is_accepted(self):
        # Blank falls back to the invitation's template; the pattern must not
        # reject the empty value.
        serializer = SubmitRequestSerializer(data={"project_name": ""})
        self.assertTrue(serializer.is_valid(), serializer.errors)
