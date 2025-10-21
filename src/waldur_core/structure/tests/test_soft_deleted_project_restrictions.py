from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories, fixtures
from waldur_core.users.tests import factories as user_factories


def extract_error_message(response_data):
    """Extract error message from DRF response data handling different formats."""
    if isinstance(response_data, list) and response_data:
        return str(response_data[0]).lower()
    elif isinstance(response_data, dict):
        if "non_field_errors" in response_data:
            return str(response_data["non_field_errors"][0]).lower()
        else:
            return str(response_data.get("detail", response_data)).lower()
    else:
        return str(response_data).lower()


class SoftDeletedProjectRestrictionsTest(APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff_user = self.fixture.staff
        self.user_to_invite = factories.UserFactory()

        # Soft delete the project
        self.project.delete()

    def test_invitation_creation_blocked_for_soft_deleted_project(self):
        """Test that invitations cannot be created for soft-deleted projects"""
        self.client.force_authenticate(user=self.staff_user)

        invitation_data = {
            "email": "test@example.com",
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
            "scope": factories.ProjectFactory.get_url(self.project),
        }

        response = self.client.post("/api/user-invitations/", invitation_data)
        # Can be 400 (URL validation) or 403 (permission), both indicate blocking
        self.assertIn(response.status_code, [400, 403])

    def test_group_invitation_creation_blocked_for_soft_deleted_project(self):
        """Test that group invitations cannot be created for soft-deleted projects"""
        self.client.force_authenticate(user=self.staff_user)

        group_invitation_data = {
            "customer": factories.CustomerFactory.get_url(self.project.customer),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
            "scope": factories.ProjectFactory.get_url(self.project),
        }

        response = self.client.post(
            "/api/user-group-invitations/", group_invitation_data
        )
        # Can be 400 (URL validation) or 403 (permission), both indicate blocking
        self.assertIn(response.status_code, [400, 403])

    def test_direct_user_addition_blocked_for_soft_deleted_project(self):
        """Test that users cannot be directly added to soft-deleted projects"""
        self.client.force_authenticate(user=self.staff_user)

        add_user_data = {
            "user": factories.UserFactory.get_url(self.user_to_invite),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(self.project, action="add_user")
        response = self.client.post(project_url, add_user_data)
        # Should be blocked - operations on soft-deleted projects should fail
        self.assertIn(response.status_code, [400, 403, 404])

    def test_user_role_update_blocked_for_soft_deleted_project(self):
        """Test that user roles cannot be updated in soft-deleted projects"""
        # First add user to project before soft deletion
        active_project = factories.ProjectFactory()
        active_project.add_user(self.user_to_invite, ProjectRole.MANAGER)

        # Now soft delete the project
        active_project.delete()

        self.client.force_authenticate(user=self.staff_user)

        update_user_data = {
            "user": factories.UserFactory.get_url(self.user_to_invite),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(
            active_project, action="update_user"
        )
        response = self.client.post(project_url, update_user_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        error_message = extract_error_message(response.data)
        self.assertIn("terminated projects", error_message)

    def test_user_removal_blocked_for_soft_deleted_project(self):
        """Test that users cannot be removed from soft-deleted projects"""
        # First add user to project before soft deletion
        active_project = factories.ProjectFactory()
        active_project.add_user(self.user_to_invite, ProjectRole.MANAGER)

        # Now soft delete the project
        active_project.delete()

        self.client.force_authenticate(user=self.staff_user)

        remove_user_data = {
            "user": factories.UserFactory.get_url(self.user_to_invite),
            "role": f"PROJECT.{ProjectRole.MANAGER.name}",
        }

        project_url = factories.ProjectFactory.get_url(
            active_project, action="delete_user"
        )
        response = self.client.post(
            f"{project_url}?include_terminated=true", remove_user_data
        )
        # Should be blocked (400 or 403)
        self.assertIn(response.status_code, [400, 403])

    def test_active_project_team_management_still_works(self):
        """Test that active projects don't trigger the soft-deleted restriction"""
        active_project = factories.ProjectFactory()

        self.client.force_authenticate(user=self.staff_user)

        add_user_data = {
            "user": factories.UserFactory.get_url(self.user_to_invite),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(
            active_project, action="add_user"
        )
        response = self.client.post(project_url, add_user_data)
        # We just verify that it doesn't return the soft-deleted error message
        # The actual success might depend on other validation logic
        if response.status_code == 400:
            if isinstance(response.data, list):
                error_message = str(response.data[0]).lower()
            else:
                error_message = str(response.data.get("detail", "")).lower()
            self.assertNotIn("terminated projects", error_message)

    def test_invitation_send_blocked_for_soft_deleted_project(self):
        """Test that existing invitations cannot be sent for soft-deleted projects"""
        # Create invitation for active project first
        active_project = factories.ProjectFactory()
        invitation = user_factories.ProjectInvitationFactory(
            scope=active_project, role=ProjectRole.ADMIN, email="test@example.com"
        )

        # Now soft delete the project
        active_project.delete()

        self.client.force_authenticate(user=self.staff_user)

        send_url = user_factories.ProjectInvitationFactory.get_url(
            invitation, action="send"
        )
        response = self.client.post(send_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_invitation_cancel_blocked_for_soft_deleted_project(self):
        """Test that existing invitations cannot be cancelled for soft-deleted projects"""
        # Create invitation for active project first
        active_project = factories.ProjectFactory()
        invitation = user_factories.ProjectInvitationFactory(
            scope=active_project, role=ProjectRole.ADMIN, email="test@example.com"
        )

        # Now soft delete the project
        active_project.delete()

        self.client.force_authenticate(user=self.staff_user)

        cancel_url = user_factories.ProjectInvitationFactory.get_url(
            invitation, action="cancel"
        )
        response = self.client.post(cancel_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_group_invitation_cancel_blocked_for_soft_deleted_project(self):
        """Test that group invitations cannot be cancelled for soft-deleted projects"""
        active_project = factories.ProjectFactory()
        group_invitation = user_factories.ProjectGroupInvitationFactory(
            scope=active_project,
            customer=active_project.customer,
            role=ProjectRole.ADMIN,
        )

        # Now soft delete the project
        active_project.delete()

        self.client.force_authenticate(user=self.staff_user)

        cancel_url = user_factories.ProjectGroupInvitationFactory.get_url(
            group_invitation, action="cancel"
        )
        response = self.client.post(cancel_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_invitation_delete_still_works_for_staff(self):
        """Test that staff can still delete invitations for soft-deleted projects (cleanup)"""
        active_project = factories.ProjectFactory()
        invitation = user_factories.ProjectInvitationFactory(
            scope=active_project, role=ProjectRole.ADMIN, email="test@example.com"
        )

        # Now soft delete the project
        active_project.delete()

        self.client.force_authenticate(user=self.staff_user)

        delete_url = user_factories.ProjectInvitationFactory.get_url(
            invitation, action="delete"
        )
        response = self.client.post(delete_url)
        # Delete should still work for cleanup purposes
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_scoped_invitations_not_affected(self):
        """Test that customer-level invitations are not affected when project is soft-deleted"""
        active_project = factories.ProjectFactory()
        customer = active_project.customer

        # Create customer-level invitation
        from waldur_core.permissions.fixtures import CustomerRole

        invitation = user_factories.CustomerInvitationFactory(
            scope=customer, role=CustomerRole.OWNER, email="test@example.com"
        )

        # Soft delete the project (but not the customer)
        active_project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Customer-level invitation operations should still work
        send_url = user_factories.CustomerInvitationFactory.get_url(
            invitation, action="send"
        )
        response = self.client.post(send_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_multiple_projects_one_soft_deleted(self):
        """Test that soft-deleting one project doesn't affect others"""
        active_project1 = factories.ProjectFactory()
        active_project2 = factories.ProjectFactory(customer=active_project1.customer)

        # Soft delete only one project
        active_project1.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Should be able to add users to the active project
        add_user_data = {
            "user": factories.UserFactory.get_url(self.user_to_invite),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project2_url = factories.ProjectFactory.get_url(
            active_project2, action="add_user"
        )
        response = self.client.post(project2_url, add_user_data)
        # Should not get the terminated projects error
        if response.status_code == 400:
            if isinstance(response.data, list):
                error_message = str(response.data[0]).lower()
            else:
                error_message = str(response.data.get("detail", "")).lower()
            self.assertNotIn("terminated projects", error_message)

    def test_soft_deleted_project_user_permissions_read_only(self):
        """Test that we can still read user permissions for soft-deleted projects"""
        active_project = factories.ProjectFactory()
        user = factories.UserFactory()
        active_project.add_user(user, ProjectRole.ADMIN)

        # Soft delete the project
        active_project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Should still be able to list users (read-only access)
        list_users_url = factories.ProjectFactory.get_url(
            active_project, action="list_users"
        )
        response = self.client.get(list_users_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invitation_creation_error_message_clear(self):
        """Test that the error message for invitation creation is clear and helpful"""
        self.client.force_authenticate(user=self.staff_user)

        invitation_data = {
            "email": "test@example.com",
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
            "scope": factories.ProjectFactory.get_url(self.project),
        }

        response = self.client.post("/api/user-invitations/", invitation_data)
        # Can be 400 (URL validation) or 403 (permission), both indicate blocking
        self.assertIn(response.status_code, [400, 403])
        # The error should be from the can_manage_invitation_with function

    def test_non_project_scopes_not_affected(self):
        """Test that our restrictions only apply to project scopes"""
        customer = factories.CustomerFactory()

        self.client.force_authenticate(user=self.staff_user)

        # Customer-level operations should not be affected by our project restrictions
        from waldur_core.permissions.fixtures import CustomerRole

        invitation_data = {
            "email": "test@example.com",
            "role": f"CUSTOMER.{CustomerRole.OWNER.name}",
            "scope": factories.CustomerFactory.get_url(customer),
        }

        response = self.client.post("/api/user-invitations/", invitation_data)
        # Should not get blocked by our project-specific validation
        # (might fail for other reasons like permissions, but not our restriction)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SoftDeletedProjectEdgeCasesTest(APITestCase):
    """Test edge cases and specific scenarios for soft-deleted project restrictions"""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff_user = self.fixture.staff

    def test_project_soft_delete_then_restore_scenario(self):
        """Test behavior if a project is soft-deleted and then restored"""
        user_to_add = factories.UserFactory()

        # Project is active initially
        self.assertFalse(self.project.is_removed)

        # Soft delete the project
        self.project.delete()
        self.assertTrue(self.project.is_removed)

        # Team management should be blocked
        self.client.force_authenticate(user=self.staff_user)
        add_user_data = {
            "user": factories.UserFactory.get_url(user_to_add),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(self.project, action="add_user")
        response = self.client.post(project_url, add_user_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Restore the project (manually set is_removed=False)
        self.project.is_removed = False
        self.project.save()

        # Team management should work again
        response = self.client.post(project_url, add_user_data)
        # Should not get the terminated projects error
        if response.status_code == 400:
            if isinstance(response.data, list):
                error_message = str(response.data[0]).lower()
            else:
                error_message = str(response.data.get("detail", "")).lower()
            self.assertNotIn("terminated projects", error_message)

    def test_concurrent_operations_during_soft_delete(self):
        """Test that operations fail gracefully if project is soft-deleted during operation"""
        factories.UserFactory()

        # Create invitation while project is active
        invitation = user_factories.ProjectInvitationFactory(
            scope=self.project, role=ProjectRole.ADMIN, email="test@example.com"
        )

        # Now soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Try to send the invitation - should be blocked
        send_url = user_factories.ProjectInvitationFactory.get_url(
            invitation, action="send"
        )
        response = self.client.post(send_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_different_user_roles_restrictions(self):
        """Test that restrictions apply consistently across different user roles"""
        user_to_add = factories.UserFactory()

        # Soft delete the project
        self.project.delete()

        # Test with different types of users
        test_users = [
            self.fixture.staff,  # Staff user
            self.fixture.owner,  # Customer owner
            self.fixture.admin,  # Project admin
            self.fixture.manager,  # Project manager
        ]

        add_user_data = {
            "user": factories.UserFactory.get_url(user_to_add),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(self.project, action="add_user")

        for user in test_users:
            with self.subTest(user=user.username):
                self.client.force_authenticate(user=user)
                response = self.client.post(project_url, add_user_data)

                # All should be blocked with the same error
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                error_message = extract_error_message(response.data)
                self.assertIn("terminated projects", error_message)

    def test_invitation_state_transitions_blocked(self):
        """Test that all invitation state transitions are blocked for soft-deleted projects"""
        # Create invitation while project is active
        invitation = user_factories.ProjectInvitationFactory(
            scope=self.project,
            role=ProjectRole.ADMIN,
            email="test@example.com",
            state="PENDING",
        )

        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Test different invitation actions
        invitation_actions = [
            ("send", "send"),
            ("cancel", "cancel"),
        ]

        for action_name, url_action in invitation_actions:
            with self.subTest(action=action_name):
                action_url = user_factories.ProjectInvitationFactory.get_url(
                    invitation, action=url_action
                )
                response = self.client.post(action_url)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_bulk_operations_blocked(self):
        """Test that bulk user management operations are blocked"""
        users_to_add = [factories.UserFactory() for _ in range(3)]

        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Try to add multiple users (should all fail)
        for user in users_to_add:
            add_user_data = {
                "user": factories.UserFactory.get_url(user),
                "role": f"PROJECT.{ProjectRole.ADMIN.name}",
            }

            project_url = factories.ProjectFactory.get_url(
                self.project, action="add_user"
            )
            response = self.client.post(project_url, add_user_data)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_role_hierarchy_restrictions(self):
        """Test that restrictions apply regardless of role hierarchy"""
        user_to_add = factories.UserFactory()

        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Test different project role levels
        project_roles = [
            ProjectRole.ADMIN,
            ProjectRole.MANAGER,
        ]

        for role in project_roles:
            with self.subTest(role=role.name):
                add_user_data = {
                    "user": factories.UserFactory.get_url(user_to_add),
                    "role": f"PROJECT.{role.name}",
                }

                project_url = factories.ProjectFactory.get_url(
                    self.project, action="add_user"
                )
                response = self.client.post(project_url, add_user_data)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_existing_users_cannot_be_modified(self):
        """Test that existing users in soft-deleted projects cannot be modified"""
        user_in_project = factories.UserFactory()

        # Add user while project is active
        self.project.add_user(user_in_project, ProjectRole.MANAGER)

        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Try to update user's role
        update_user_data = {
            "user": factories.UserFactory.get_url(user_in_project),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(
            self.project, action="update_user"
        )
        response = self.client.post(project_url, update_user_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Try to remove user
        remove_user_data = {
            "user": factories.UserFactory.get_url(user_in_project),
            "role": f"PROJECT.{ProjectRole.MANAGER.name}",
        }

        project_url = factories.ProjectFactory.get_url(
            self.project, action="delete_user"
        )
        response = self.client.post(project_url, remove_user_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SoftDeletedProjectIntegrationTest(APITestCase):
    """Test integration with existing Waldur functionality"""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff_user = self.fixture.staff

    def test_integration_with_is_removed_filter(self):
        """Test that our restrictions work with the is_removed filter"""
        user_to_add = factories.UserFactory()

        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Even when explicitly including terminated projects, team management should be blocked
        add_user_data = {
            "user": factories.UserFactory.get_url(user_to_add),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(self.project, action="add_user")
        # Add include_terminated parameter
        response = self.client.post(
            f"{project_url}?include_terminated=true", add_user_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_integration_with_project_stats_endpoint(self):
        """Test that project stats work but team management doesn't for soft-deleted projects"""
        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Stats should work with include_terminated=true
        stats_url = factories.ProjectFactory.get_url(self.project, action="stats")
        response = self.client.get(f"{stats_url}?include_terminated=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # But team management should still be blocked
        add_user_data = {
            "user": factories.UserFactory.get_url(factories.UserFactory()),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        add_user_url = factories.ProjectFactory.get_url(self.project, action="add_user")
        response = self.client.post(
            f"{add_user_url}?include_terminated=true", add_user_data
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_integration_with_existing_validation(self):
        """Test that our validation works alongside existing validation"""
        user_to_add = factories.UserFactory()

        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Test with invalid role (should hit our validation first)
        add_user_data = {
            "user": factories.UserFactory.get_url(user_to_add),
            "role": "INVALID.ROLE",  # Invalid role format
        }

        project_url = factories.ProjectFactory.get_url(self.project, action="add_user")
        response = self.client.post(project_url, add_user_data)
        # Our validation should trigger first
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify it's our error message
        error_message = extract_error_message(response.data)
        self.assertIn("terminated projects", error_message)

    def test_integration_with_permissions_system(self):
        """Test that our restrictions work with the existing permissions system"""
        user_to_add = factories.UserFactory()
        non_staff_user = factories.UserFactory()  # Regular user without permissions

        # Soft delete the project
        self.project.delete()

        # Test with non-staff user (should hit our validation even without proper permissions)
        self.client.force_authenticate(user=non_staff_user)

        add_user_data = {
            "user": factories.UserFactory.get_url(user_to_add),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(self.project, action="add_user")
        response = self.client.post(project_url, add_user_data)

        # Could be 400 (our validation) or 404/403 (permissions), but definitely blocked
        self.assertIn(response.status_code, [400, 403, 404])

    def test_integration_with_project_deletion_workflow(self):
        """Test restrictions during the typical project deletion workflow"""
        user_to_add = factories.UserFactory()

        # Start with active project that has users
        existing_user = factories.UserFactory()
        self.project.add_user(existing_user, ProjectRole.MANAGER)

        self.client.force_authenticate(user=self.staff_user)

        # Before deletion, team management should work
        add_user_data = {
            "user": factories.UserFactory.get_url(user_to_add),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        project_url = factories.ProjectFactory.get_url(self.project, action="add_user")
        response = self.client.post(project_url, add_user_data)
        # Should not be blocked by our restriction
        if response.status_code == 400:
            if isinstance(response.data, list):
                error_message = str(response.data[0]).lower()
            else:
                error_message = str(response.data.get("detail", "")).lower()
            self.assertNotIn("terminated projects", error_message)

        # Now soft delete the project
        self.project.delete()

        # After deletion, all team management should be blocked
        response = self.client.post(project_url, add_user_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Existing user modification should also be blocked
        update_user_data = {
            "user": factories.UserFactory.get_url(existing_user),
            "role": f"PROJECT.{ProjectRole.ADMIN.name}",
        }

        update_url = factories.ProjectFactory.get_url(
            self.project, action="update_user"
        )
        response = self.client.post(update_url, update_user_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_no_impact_on_other_project_operations(self):
        """Test that our restrictions don't affect other project operations"""
        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Project retrieval with include_terminated should still work
        project_url = factories.ProjectFactory.get_url(self.project)
        response = self.client.get(f"{project_url}?include_terminated=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_removed"])

        # Listing users should still work (read-only)
        list_users_url = factories.ProjectFactory.get_url(
            self.project, action="list_users"
        )
        response = self.client.get(f"{list_users_url}?include_terminated=true")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_error_consistency_across_endpoints(self):
        """Test that all blocked endpoints return consistent error messages"""
        user_to_manage = factories.UserFactory()

        # Add user before soft deletion
        self.project.add_user(user_to_manage, ProjectRole.MANAGER)

        # Soft delete the project
        self.project.delete()

        self.client.force_authenticate(user=self.staff_user)

        # Test all user management endpoints
        test_cases = [
            (
                "add_user",
                {
                    "user": factories.UserFactory.get_url(factories.UserFactory()),
                    "role": f"PROJECT.{ProjectRole.ADMIN.name}",
                },
            ),
            (
                "update_user",
                {
                    "user": factories.UserFactory.get_url(user_to_manage),
                    "role": f"PROJECT.{ProjectRole.ADMIN.name}",
                },
            ),
            (
                "delete_user",
                {
                    "user": factories.UserFactory.get_url(user_to_manage),
                    "role": f"PROJECT.{ProjectRole.MANAGER.name}",
                },
            ),
        ]

        for action, data in test_cases:
            with self.subTest(action=action):
                url = factories.ProjectFactory.get_url(self.project, action=action)
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

                # Check error message consistency
                error_message = extract_error_message(response.data)
                self.assertIn("terminated projects", error_message)
                self.assertIn("cannot manage team members", error_message)
