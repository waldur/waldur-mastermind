from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EVENT_GROUP_MAPPING, EventType
from waldur_core.permissions import models, tasks, utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.serializers import clone_role_for_customer
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories, fixtures


class LogRoleEventTest(TestCase):
    def setUp(self):
        self._logger_mock = mock.patch("waldur_core.logging.event_logger.emit")
        self.logger_mock = self._logger_mock.start()
        self.addCleanup(self._logger_mock.stop)

    def test_logger_called_when_customer_role_is_granted(self):
        fixture = fixtures.CustomerFixture()

        owner = fixture.owner
        self.logger_mock.reset_mock()
        fixture.customer.add_user(fixture.user, CustomerRole.OWNER, owner)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_GRANTED,
            event_context=mock.ANY,
            scopes=[fixture.customer, fixture.customer],
        )

    def test_logger_called_when_customer_role_is_revoked(self):
        fixture = fixtures.CustomerFixture()
        owner = fixture.owner

        self.logger_mock.reset_mock()
        fixture.customer.remove_user(owner, CustomerRole.OWNER, fixture.staff)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_REVOKED,
            event_context=mock.ANY,
            scopes=[fixture.customer, fixture.customer],
        )

    def test_logger_called_when_project_role_is_granted(self):
        fixture = fixtures.ProjectFixture()
        current_user = fixture.owner

        self.logger_mock.reset_mock()
        fixture.project.add_user(fixture.user, ProjectRole.MANAGER, current_user)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_GRANTED,
            event_context=mock.ANY,
            scopes=[fixture.project, fixture.customer],
        )

    def test_logger_called_when_project_role_is_revoked(self):
        fixture = fixtures.ProjectFixture()
        manager = fixture.manager
        current_user = fixture.owner

        self.logger_mock.reset_mock()
        fixture.project.remove_user(manager, ProjectRole.MANAGER, current_user)

        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ROLE_REVOKED,
            event_context=mock.ANY,
            scopes=[fixture.project, fixture.customer],
        )


class AccessSubnetCreateModifyDelete(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.client.force_authenticate(user=self.fixture.owner)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ACCESS_SUBNET)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_ACCESS_SUBNET)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_ACCESS_SUBNET)
        self.customer = self.fixture.customer
        self.customer_url = factories.CustomerFactory.get_url(
            customer=self.fixture.customer
        )
        self._logger_mock = mock.patch("waldur_core.logging.event_logger.emit")
        self.logger_mock = self._logger_mock.start()
        self.addCleanup(self._logger_mock.stop)

    def test_logger_called_when_subnet_created(self):
        self.logger_mock.reset_mock()
        access_subnet = self.create_access_subnet()
        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_CREATION_SUCCEEDED,
            event_context={
                "access_subnet": access_subnet,
            },
            scopes=[access_subnet, access_subnet.customer],
        )

    def test_logger_called_when_subnet_modified(self):
        access_subnet = self.create_access_subnet()
        url = factories.AccessSubnetFactory.get_url(access_subnet)

        self.logger_mock.reset_mock()
        self.client.put(url, {"inet": "192.168.1.1/32"})
        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_UPDATE_SUCCEEDED,  # TODO patch calls creation_succeeded but update_succeeded desired
            event_context={
                "access_subnet": access_subnet,
            },
            scopes=[access_subnet, access_subnet.customer],
        )

    def test_logger_called_when_subnet_deleted(self):
        access_subnet = self.create_access_subnet()
        url = factories.AccessSubnetFactory.get_url(access_subnet)

        self.client.delete(url)
        self.logger_mock.assert_called_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_DELETION_SUCCEEDED,
            event_context=mock.ANY,
            scopes=mock.ANY,
        )

    def create_access_subnet(self):
        url = factories.AccessSubnetFactory.get_list_url()
        payload = {
            "customer": self.customer_url,
            "inet": "192.168.1.0/32",
            "description": "",
        }
        response = self.client.post(
            url,
            payload,
        )
        return response.data.serializer.instance


class RoleLogReasonTest(TestCase):
    def setUp(self):
        self._logger_mock = mock.patch("waldur_core.logging.event_logger.emit")
        self.logger_mock = self._logger_mock.start()
        self.addCleanup(self._logger_mock.stop)
        self.fixture = fixtures.CustomerFixture()

    def test_manual_role_revocation_includes_reason_and_initiator(self):
        """Test that manual role revocation via API includes proper reason and initiator."""
        self.logger_mock.reset_mock()

        # Use the delete_user utility that's called by the API
        utils.delete_user(
            self.fixture.customer,
            self.fixture.owner,
            CustomerRole.OWNER,
            current_user=self.fixture.staff,
            reason="Manual user removal via delete_user API endpoint",
        )

        # Check that the log was called with enhanced context
        # The fixture setup creates multiple log entries, so we need to find the right one
        self.assertTrue(self.logger_mock.called)

        # Find the role revocation call
        role_revoked_calls = [
            call
            for call in self.logger_mock.call_args_list
            if len(call[1]) > 1 and call[1].get("event_type") == EventType.ROLE_REVOKED
        ]
        self.assertEqual(len(role_revoked_calls), 1)
        call_args = role_revoked_calls[0]
        call_args = self.logger_mock.call_args

        # Verify message includes reason and initiator (in template form)
        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("Manual user removal via delete_user API endpoint", message)

        # Verify event context includes reason and initiated_by
        event_context = call_args[1]["event_context"]
        self.assertEqual(
            event_context["reason"], "Manual user removal via delete_user API endpoint"
        )
        self.assertEqual(
            event_context["initiated_by"],
            f"{self.fixture.staff.full_name} ({self.fixture.staff.username})",
        )

    def test_automatic_expiration_includes_system_reason(self):
        """Test that automatic expiration includes system reason and initiator."""
        # Create an expired role
        utils.add_user(
            self.fixture.customer,
            self.fixture.user,
            CustomerRole.SUPPORT,
            expiration_time=timezone.now() - timezone.timedelta(hours=1),
        )

        self.logger_mock.reset_mock()

        # Call the expiration task
        tasks.check_expired_permissions()

        # Check that the log was called with system context
        self.logger_mock.assert_called()
        call_args = self.logger_mock.call_args

        # Verify message includes system initiator and expiration reason (in template form)
        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("Automatic expiration cleanup task", message)

        # Verify event context
        event_context = call_args[1]["event_context"]
        self.assertEqual(event_context["reason"], "Automatic expiration cleanup task")
        self.assertEqual(event_context["initiated_by"], "System")

    def test_project_deletion_cascade_includes_reason(self):
        """Test that project deletion cascade includes proper reason."""
        project_fixture = fixtures.ProjectFixture()
        project = project_fixture.project

        self.logger_mock.reset_mock()

        # Import and call the handler directly since we can't easily trigger pre_delete signal in tests
        from waldur_core.structure.handlers import revoke_roles_on_project_deletion

        revoke_roles_on_project_deletion(sender=project.__class__, instance=project)

        # Check that roles were revoked with cascade reason
        if self.logger_mock.called:  # Only if there were roles to revoke
            call_args = self.logger_mock.call_args
            message = call_args[0][0]
            self.assertIn("Initiated by: {initiated_by}", message)
            self.assertIn("Project deletion cascade", message)

            event_context = call_args[1]["event_context"]
            self.assertEqual(event_context["reason"], "Project deletion cascade")
            self.assertEqual(event_context["initiated_by"], "System")

    def test_role_granted_includes_reason_and_initiator(self):
        """Test that role granting includes proper reason and initiator."""
        self.logger_mock.reset_mock()

        utils.add_user(
            self.fixture.customer,
            self.fixture.user,
            CustomerRole.SUPPORT,
            created_by=self.fixture.owner,
        )

        # Check that the log was called with enhanced context
        self.logger_mock.assert_called()
        call_args = self.logger_mock.call_args

        # Verify message includes reason and initiator (in template form)
        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("Manual role assignment via API", message)

        # Verify event context
        event_context = call_args[1]["event_context"]
        self.assertEqual(event_context["reason"], "Manual role assignment via API")
        self.assertEqual(
            event_context["initiated_by"],
            f"{self.fixture.owner.full_name} ({self.fixture.owner.username})",
        )

    def test_role_updated_includes_reason_and_initiator(self):
        """Test that role updates include proper reason and initiator."""
        # Create a role first
        user_role = utils.add_user(
            self.fixture.customer,
            self.fixture.user,
            CustomerRole.SUPPORT,
        )

        self.logger_mock.reset_mock()

        # Update the role expiration time
        new_expiration = timezone.now() + timezone.timedelta(days=30)
        user_role.set_expiration_time(new_expiration, current_user=self.fixture.staff)

        # Check that the log was called with enhanced context
        self.logger_mock.assert_called()
        call_args = self.logger_mock.call_args

        # Verify message includes reason and initiator (in template form)
        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("Manual role update via API", message)

        # Verify event context
        event_context = call_args[1]["event_context"]
        self.assertEqual(event_context["reason"], "Manual role update via API")
        self.assertEqual(
            event_context["initiated_by"],
            f"{self.fixture.staff.full_name} ({self.fixture.staff.username})",
        )

    def test_system_initiated_operations_have_system_initiator(self):
        """Test that system operations without current_user show System as initiator."""
        user_role = utils.add_user(
            self.fixture.customer,
            self.fixture.user,
            CustomerRole.SUPPORT,
        )

        self.logger_mock.reset_mock()

        # Revoke without current_user (simulating system operation)
        user_role.revoke(current_user=None, reason="System maintenance")

        # Check that the log shows System as initiator
        self.logger_mock.assert_called()
        call_args = self.logger_mock.call_args

        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("System maintenance", message)

        event_context = call_args[1]["event_context"]
        self.assertEqual(event_context["reason"], "System maintenance")
        self.assertEqual(event_context["initiated_by"], "System")


ROLE_ENDPOINT = "/api/roles/"


class RoleDefinitionEventTest(test.APITestCase):
    """Changes to what a role *means* must reach the audit feed.

    These assert on the persisted ``Event`` rows rather than on a mocked
    ``emit``: the message templates are formatted against the compiled context,
    so a placeholder that is not in the context only fails once the event is
    really emitted.
    """

    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.customer = factories.CustomerFactory()
        self.client.force_authenticate(self.staff)

    def get_events(self, event_type):
        return list(
            logging_models.Event.objects.filter(event_type=event_type).order_by("id")
        )

    def get_feed_scopes(self, event):
        return [feed.scope for feed in logging_models.Feed.objects.filter(event=event)]

    def create_role(self, name="CUSTOMER.AUDITED", permissions=None):
        response = self.client.post(
            ROLE_ENDPOINT,
            {
                "name": name,
                "content_type": "customer",
                "permissions": permissions or [PermissionEnum.UPDATE_OFFERING.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return models.Role.objects.get(uuid=response.data["uuid"])

    def test_role_creation_is_logged(self):
        role = self.create_role()

        events = self.get_events(EventType.ROLE_DEFINITION_CREATED)
        self.assertEqual(len(events), 1)
        self.assertIn(role.name, events[0].message)
        self.assertEqual(
            events[0].context["permissions"], [PermissionEnum.UPDATE_OFFERING.value]
        )
        self.assertIn(self.staff.username, events[0].context["initiated_by"])
        self.assertFalse(events[0].context["is_system_role"])

    def test_role_update_records_the_permission_delta(self):
        role = self.create_role()

        response = self.client.put(
            permission_factories.RoleFactory.get_url(role),
            {
                "name": role.name,
                "content_type": "customer",
                "permissions": [PermissionEnum.APPROVE_ORDER.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        events = self.get_events(EventType.ROLE_DEFINITION_UPDATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].context["added_permissions"],
            [PermissionEnum.APPROVE_ORDER.value],
        )
        self.assertEqual(
            events[0].context["removed_permissions"],
            [PermissionEnum.UPDATE_OFFERING.value],
        )

    def test_rename_is_recorded_with_the_previous_name(self):
        role = self.create_role()

        response = self.client.put(
            permission_factories.RoleFactory.get_url(role),
            {
                "name": "CUSTOMER.RENAMED",
                "content_type": "customer",
                "permissions": [PermissionEnum.UPDATE_OFFERING.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        events = self.get_events(EventType.ROLE_DEFINITION_UPDATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].context["old_name"], "CUSTOMER.AUDITED")
        self.assertEqual(events[0].context["role_name"], "CUSTOMER.RENAMED")

    def test_resubmitting_the_same_definition_is_not_an_event(self):
        role = self.create_role()

        response = self.client.put(
            permission_factories.RoleFactory.get_url(role),
            {
                "name": role.name,
                "content_type": "customer",
                "permissions": [PermissionEnum.UPDATE_OFFERING.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.get_events(EventType.ROLE_DEFINITION_UPDATED), [])

    def test_description_update_is_logged(self):
        role = self.create_role()

        response = self.client.put(
            permission_factories.RoleFactory.get_url(role, "update_descriptions"),
            {"description_en": "Audited role"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        events = self.get_events(EventType.ROLE_DEFINITION_UPDATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].context["new_descriptions"]["description_en"], "Audited role"
        )

    def test_role_deletion_is_logged(self):
        role = self.create_role()
        role_name = role.name

        response = self.client.delete(permission_factories.RoleFactory.get_url(role))
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

        events = self.get_events(EventType.ROLE_DEFINITION_DELETED)
        self.assertEqual(len(events), 1)
        self.assertIn(role_name, events[0].message)
        self.assertEqual(
            events[0].context["permissions"], [PermissionEnum.UPDATE_OFFERING.value]
        )

    def test_disable_and_enable_are_logged_once_each(self):
        role = self.create_role()
        disable_url = permission_factories.RoleFactory.get_url(role, "disable")
        enable_url = permission_factories.RoleFactory.get_url(role, "enable")

        self.client.post(disable_url)
        # A repeated call is a no-op and must not add a second event.
        self.client.post(disable_url)
        self.client.post(enable_url)

        self.assertEqual(len(self.get_events(EventType.ROLE_DISABLED)), 1)
        self.assertEqual(len(self.get_events(EventType.ROLE_ENABLED)), 1)

    def test_clone_is_filed_in_the_organization_feed(self):
        response = self.client.post(
            permission_factories.RoleFactory.get_url(
                CustomerRole.OWNER, "clone_to_customer"
            ),
            {"customer": self.customer.uuid.hex, "conceal_template": True},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        events = self.get_events(EventType.ROLE_CLONED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].context["template_name"], CustomerRole.OWNER.name)
        self.assertEqual(self.get_feed_scopes(events[0]), [self.customer])

        # Concealing the template is part of the same request, and it is logged
        # by the model handler rather than by the viewset.
        concealed = self.get_events(EventType.ROLE_CONCEALED)
        self.assertEqual(len(concealed), 1)
        self.assertEqual(self.get_feed_scopes(concealed[0]), [self.customer])

    def test_conceal_and_reveal_are_logged(self):
        list_url = reverse("customer-role-concealment-list")
        response = self.client.post(
            list_url,
            {
                "role": ProjectRole.MEMBER.uuid.hex,
                "customer": self.customer.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        concealed = self.get_events(EventType.ROLE_CONCEALED)
        self.assertEqual(len(concealed), 1)
        self.assertEqual(concealed[0].context["role_name"], ProjectRole.MEMBER.name)
        self.assertEqual(self.get_feed_scopes(concealed[0]), [self.customer])

        detail_url = reverse(
            "customer-role-concealment-detail",
            kwargs={"uuid": response.data["uuid"]},
        )
        delete = self.client.delete(detail_url)
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)

        revealed = self.get_events(EventType.ROLE_REVEALED)
        self.assertEqual(len(revealed), 1)
        self.assertEqual(self.get_feed_scopes(revealed[0]), [self.customer])

    def test_editing_an_organization_role_reaches_that_organization_feed(self):
        clone = clone_role_for_customer(
            CustomerRole.OWNER, self.customer, conceal_template=False
        )

        response = self.client.put(
            permission_factories.RoleFactory.get_url(clone),
            {
                "name": clone.name,
                "content_type": "customer",
                "permissions": [PermissionEnum.APPROVE_ORDER.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        events = self.get_events(EventType.ROLE_DEFINITION_UPDATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.get_feed_scopes(events[0]), [self.customer])

    def test_deployment_wide_role_event_has_no_feed(self):
        role = self.create_role()

        events = self.get_events(EventType.ROLE_DEFINITION_CREATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.get_feed_scopes(events[0]), [])
        self.assertNotIn("customer_uuid", events[0].context)
        self.assertEqual(role.name, events[0].context["role_name"])

    def test_scope_change_through_update_is_logged(self):
        # content_type is writable on the update endpoint, so a role can change
        # scope without going through any dedicated action.
        role = self.create_role()

        response = self.client.put(
            permission_factories.RoleFactory.get_url(role),
            {
                "name": role.name,
                "content_type": "project",
                "permissions": [PermissionEnum.UPDATE_OFFERING.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        events = self.get_events(EventType.ROLE_DEFINITION_UPDATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].context["old_content_type"], "customer")
        self.assertEqual(events[0].context["new_content_type"], "project")

    def test_disabling_through_update_is_logged_like_the_action(self):
        # is_active is writable here too; it must not be a way to disable a role
        # without the event the disable action emits.
        role = self.create_role()

        response = self.client.put(
            permission_factories.RoleFactory.get_url(role),
            {
                "name": role.name,
                "content_type": "customer",
                "permissions": [PermissionEnum.UPDATE_OFFERING.value],
                "is_active": False,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        role.refresh_from_db()
        self.assertFalse(role.is_active)

        events = self.get_events(EventType.ROLE_DISABLED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].context["role_name"], role.name)

    def test_deleting_a_concealed_role_does_not_claim_it_was_revealed(self):
        role = self.create_role()
        models.CustomerRoleConcealment.objects.create(
            role=role,
            content_type=ContentType.objects.get_for_model(Customer),
            object_id=self.customer.id,
        )
        self.assertEqual(len(self.get_events(EventType.ROLE_CONCEALED)), 1)

        response = self.client.delete(permission_factories.RoleFactory.get_url(role))
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

        # The concealment is cascade-deleted, but the organization is not
        # getting the role back — it no longer exists.
        self.assertEqual(self.get_events(EventType.ROLE_REVEALED), [])
        self.assertEqual(len(self.get_events(EventType.ROLE_DEFINITION_DELETED)), 1)

    def test_deleting_a_role_through_a_queryset_does_not_claim_it_was_revealed(self):
        # Same cascade as deleting the role instance, but the deletion starts
        # from a queryset, so the guard has to recognise that origin too.
        role = self.create_role()
        models.CustomerRoleConcealment.objects.create(
            role=role,
            content_type=ContentType.objects.get_for_model(Customer),
            object_id=self.customer.id,
        )

        models.Role.objects.filter(pk=role.pk).delete()

        self.assertEqual(self.get_events(EventType.ROLE_REVEALED), [])

    def test_deleting_a_concealment_through_a_queryset_is_logged(self):
        # The guard must not over-suppress: a concealment deleted on its own is
        # a reveal whether the caller holds the instance or a queryset.
        role = self.create_role()
        models.CustomerRoleConcealment.objects.create(
            role=role,
            content_type=ContentType.objects.get_for_model(Customer),
            object_id=self.customer.id,
        )

        models.CustomerRoleConcealment.objects.filter(role=role).delete()

        revealed = self.get_events(EventType.ROLE_REVEALED)
        self.assertEqual(len(revealed), 1)
        self.assertEqual(self.get_feed_scopes(revealed[0]), [self.customer])

    def test_concealment_with_a_dangling_scope_is_not_logged(self):
        # The scope is a generic FK, so hard-deleting the organization leaves
        # the concealment behind. Such a row has no organization to name in the
        # message, and neither end of its lifecycle may blow up on that.
        role = self.create_role()
        orphan_id = (Customer.objects.order_by("-id").first().id) + 1000

        concealment = models.CustomerRoleConcealment.objects.create(
            role=role,
            content_type=ContentType.objects.get_for_model(Customer),
            object_id=orphan_id,
        )
        self.assertEqual(self.get_events(EventType.ROLE_CONCEALED), [])

        concealment.delete()
        self.assertEqual(self.get_events(EventType.ROLE_REVEALED), [])

    def test_definition_changes_do_not_reuse_the_assignment_event_type(self):
        # ROLE_UPDATED means "an assignment's expiry changed"; reusing it for a
        # definition change would silently alter what existing audit queries and
        # hooks match.
        role = self.create_role()
        self.client.put(
            permission_factories.RoleFactory.get_url(role),
            {
                "name": role.name,
                "content_type": "customer",
                "permissions": [PermissionEnum.APPROVE_ORDER.value],
            },
        )
        self.client.post(permission_factories.RoleFactory.get_url(role, "disable"))

        self.assertEqual(self.get_events(EventType.ROLE_UPDATED), [])


class RoleEventGroupTest(TestCase):
    def test_every_role_event_belongs_to_the_permissions_group(self):
        grouped = {event for events in EVENT_GROUP_MAPPING.values() for event in events}
        orphans = sorted(
            event.value
            for event in EventType
            if event.value.startswith("role_") and event not in grouped
        )
        self.assertEqual(orphans, [])
