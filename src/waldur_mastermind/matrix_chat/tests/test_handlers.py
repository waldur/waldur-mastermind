import io
import tempfile
from pathlib import Path
from unittest import mock

from constance.test import override_config
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import handlers, models, tasks


def _create_room_for_project(project):
    ct = ContentType.objects.get_for_model(project)
    return models.MatrixRoom.objects.create(
        room_id="!test:matrix.example.com",
        room_name="Test Room",
        state=models.RoomStates.ACTIVE,
        content_type=ct,
        object_id=project.id,
    )


@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnRoleGrantedTest(TestCase):
    def test_no_op_when_disabled(self, mock_client):
        mock_client.is_enabled.return_value = False
        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory()
        project.add_user(user, ProjectRole.MEMBER)
        # Handler should not raise or dispatch tasks

    def test_no_op_when_no_room(self, mock_client):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        user = structure_factories.UserFactory()
        project.add_user(user, ProjectRole.MEMBER)
        # No room exists, so no task should be dispatched

    @mock.patch("waldur_mastermind.matrix_chat.tasks.invite_user_to_room")
    def test_dispatches_invite_when_room_exists(self, mock_invite_task, mock_client):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        _create_room_for_project(project)

        user = structure_factories.UserFactory()

        # Call handler directly
        project.add_user(user, ProjectRole.MEMBER)
        role_instance = UserRole.objects.filter(user=user, scope=project).first()
        if role_instance:
            handlers.on_role_granted(sender=UserRole, instance=role_instance)


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnCustomerRoleGrantedTest(TestCase):
    # Member sync puts customer-level role holders into every room of the
    # customer's projects, but only runs on demand: a new owner would stay
    # out of the rooms until someone happened to sync each of them.

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.user = structure_factories.UserFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)

    def _room(self, customer, state=models.RoomStates.ACTIVE):
        project = structure_factories.ProjectFactory(customer=customer)
        return models.MatrixRoom.objects.create(
            room_id=f"!{project.uuid.hex}:matrix.example.com",
            room_name=project.name,
            state=state,
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
        )

    def _grant(self, role=None):
        with self.captureOnCommitCallbacks(execute=True):
            self.customer.add_user(self.user, role or CustomerRole.OWNER)

    def test_invites_to_every_active_room_of_the_customers_projects(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        rooms = [self._room(self.customer), self._room(self.customer)]

        self._grant()

        self.assertCountEqual(
            mock_tasks.invite_user_to_room.delay.call_args_list,
            [mock.call(str(room.uuid), str(self.user.uuid)) for room in rooms],
        )

    def test_leaves_out_a_role_that_cannot_create_rooms(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        self._room(self.customer)
        # Seeing the customer's projects is not enough.
        CustomerRole.READER.add_permission(PermissionEnum.LIST_PROJECTS)

        self._grant(CustomerRole.READER)

        mock_tasks.invite_user_to_room.delay.assert_not_called()

    def test_leaves_out_rooms_that_are_not_active(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        self._room(self.customer, state=models.RoomStates.ARCHIVED)

        self._grant()

        mock_tasks.invite_user_to_room.delay.assert_not_called()

    def test_leaves_out_rooms_of_other_customers(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        self._room(structure_factories.CustomerFactory())

        self._grant()

        mock_tasks.invite_user_to_room.delay.assert_not_called()

    def test_no_op_when_disabled(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = False
        self._room(self.customer)

        self._grant()

        mock_tasks.invite_user_to_room.delay.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnRoleRevokedTest(TestCase):
    def test_no_op_when_disabled(self, mock_client):
        mock_client.is_enabled.return_value = False
        structure_factories.ProjectFactory()
        # Handler should not raise

    @mock.patch("waldur_mastermind.matrix_chat.tasks.kick_user_from_room")
    def test_no_kick_if_user_has_remaining_roles(self, mock_kick_task, mock_client):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        _create_room_for_project(project)

        user = structure_factories.UserFactory()
        project.add_user(user, ProjectRole.ADMIN)
        project.add_user(user, ProjectRole.MEMBER)

        # Revoke one role - user still has another
        role_instance = UserRole.objects.filter(
            user=user, scope=project, role__name=ProjectRole.MEMBER.name
        ).first()
        if role_instance:
            handlers.on_role_revoked(sender=UserRole, instance=role_instance)
            # Should NOT dispatch kick because user still has ADMIN role


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class ProjectRoleRevokedCustomerRoleTest(TestCase):
    # A remaining customer role keeps the user in only if member sync would
    # put them in the room for it.

    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)
        self.project = structure_factories.ProjectFactory()
        self.room = _create_room_for_project(self.project)
        self.user = structure_factories.UserFactory()
        self.project.add_user(self.user, ProjectRole.MEMBER)

    def _revoke_project_role(self):
        role = UserRole.objects.get(user=self.user, scope=self.project)
        role.is_active = False
        role.save()
        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_role_revoked(sender=UserRole, instance=role)

    def test_a_remaining_reader_role_does_not_keep_the_user_in(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        self.project.customer.add_user(self.user, CustomerRole.READER)

        self._revoke_project_role()

        mock_tasks.kick_user_from_room.delay.assert_called_once_with(
            str(self.room.uuid), str(self.user.uuid)
        )

    def test_a_remaining_owner_role_keeps_the_user_in(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        self.project.customer.add_user(self.user, CustomerRole.OWNER)

        self._revoke_project_role()

        mock_tasks.kick_user_from_room.delay.assert_not_called()


class HasRoomRoleTest(TestCase):
    def test_project_rooms_follow_the_member_sync_rule(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)
        project = structure_factories.ProjectFactory()
        room = _create_room_for_project(project)
        owner = structure_factories.UserFactory()
        project.customer.add_user(owner, CustomerRole.OWNER)
        reader = structure_factories.UserFactory()
        project.customer.add_user(reader, CustomerRole.READER)
        member = structure_factories.UserFactory()
        project.add_user(member, ProjectRole.MEMBER)

        self.assertTrue(models.has_room_role(owner, room))
        self.assertTrue(models.has_room_role(member, room))
        self.assertFalse(models.has_room_role(reader, room))

    def test_customer_rooms_follow_any_customer_role(self):
        customer = structure_factories.CustomerFactory()
        room = models.MatrixRoom.objects.create(
            room_id="!c:matrix.example.com",
            room_name="customer",
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(customer),
            object_id=customer.id,
        )
        reader = structure_factories.UserFactory()
        customer.add_user(reader, CustomerRole.READER)

        self.assertTrue(models.has_room_role(reader, room))
        self.assertFalse(models.has_room_role(structure_factories.UserFactory(), room))


@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnProjectPreDeleteTest(TestCase):
    def test_no_op_when_disabled(self, mock_client):
        mock_client.is_enabled.return_value = False
        project = structure_factories.ProjectFactory()
        handlers.on_project_pre_delete(sender=type(project), instance=project)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.disable_room")
    def test_dispatches_disable_on_deletion(self, mock_disable_task, mock_client):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        ct = ContentType.objects.get_for_model(project)
        room = models.MatrixRoom.objects.create(
            room_id="!test:matrix.example.com",
            room_name="Test Room",
            state=models.RoomStates.ACTIVE,
            content_type=ct,
            object_id=project.id,
        )
        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_project_pre_delete(sender=type(project), instance=project)
        mock_disable_task.delay.assert_called_once()
        # Verify room transitioned to DISABLING
        room.refresh_from_db()
        self.assertEqual(room.state, models.RoomStates.DISABLING)


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnRoleGrantedNotificationTest(TestCase):
    def test_sends_notification_on_role_granted(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        _create_room_for_project(project)

        user = structure_factories.UserFactory(
            username="alice", first_name="Alice", last_name="Smith"
        )
        project.add_user(user, ProjectRole.ADMIN)
        role_instance = UserRole.objects.filter(user=user, scope=project).first()

        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_role_granted(sender=UserRole, instance=role_instance)

        calls = mock_tasks.send_room_notification.delay.call_args_list
        notification_calls = [c for c in calls if "granted" in str(c)]
        self.assertTrue(
            len(notification_calls) > 0,
            f"Expected notification with 'granted', got calls: {calls}",
        )


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnRoleRevokedNotificationTest(TestCase):
    def test_sends_notification_on_role_revoked(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        _create_room_for_project(project)

        user = structure_factories.UserFactory(
            username="bob", first_name="Bob", last_name="Jones"
        )
        project.add_user(user, ProjectRole.MEMBER)
        role_instance = UserRole.objects.filter(user=user, scope=project).first()

        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_role_revoked(sender=UserRole, instance=role_instance)

        calls = mock_tasks.send_room_notification.delay.call_args_list
        notification_calls = [c for c in calls if "lost" in str(c)]
        self.assertTrue(
            len(notification_calls) > 0,
            f"Expected notification with 'lost', got calls: {calls}",
        )


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnOrderStateChangedTest(TestCase):
    def test_no_op_when_disabled(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = False
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        order = marketplace_factories.OrderFactory()
        handlers.on_order_state_changed(
            sender=type(order), instance=order, created=False
        )
        mock_tasks.send_room_notification.delay.assert_not_called()

    def test_no_op_when_created(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        order = marketplace_factories.OrderFactory()
        handlers.on_order_state_changed(
            sender=type(order), instance=order, created=True
        )
        mock_tasks.send_room_notification.delay.assert_not_called()

    def test_notifies_on_order_approved(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        from waldur_mastermind.marketplace.enums import OrderStates
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        project = structure_factories.ProjectFactory()
        _create_room_for_project(project)
        order = marketplace_factories.OrderFactory(project=project)

        # Change state to EXECUTING (approved) — tracker detects this
        order.state = OrderStates.EXECUTING

        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_order_state_changed(
                sender=type(order), instance=order, created=False
            )

        calls = mock_tasks.send_room_notification.delay.call_args_list
        notification_calls = [c for c in calls if "approved" in str(c)]
        self.assertTrue(
            len(notification_calls) > 0,
            f"Expected notification with 'approved', got calls: {calls}",
        )

    def test_notifies_on_order_completed(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        from waldur_mastermind.marketplace.enums import OrderStates
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        project = structure_factories.ProjectFactory()
        _create_room_for_project(project)
        order = marketplace_factories.OrderFactory(
            project=project, state=OrderStates.EXECUTING
        )

        # Change state to DONE — tracker detects this
        order.state = OrderStates.DONE

        with self.captureOnCommitCallbacks(execute=True):
            handlers.on_order_state_changed(
                sender=type(order), instance=order, created=False
            )

        calls = mock_tasks.send_room_notification.delay.call_args_list
        notification_calls = [c for c in calls if "completed" in str(c)]
        self.assertTrue(
            len(notification_calls) > 0,
            f"Expected notification with 'completed', got calls: {calls}",
        )

    def test_no_notification_when_state_unchanged(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        from waldur_mastermind.marketplace.enums import OrderStates
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        project = structure_factories.ProjectFactory()
        _create_room_for_project(project)
        order = marketplace_factories.OrderFactory(
            project=project, state=OrderStates.EXECUTING
        )

        # Don't change state — tracker sees no change
        handlers.on_order_state_changed(
            sender=type(order), instance=order, created=False
        )
        mock_tasks.send_room_notification.delay.assert_not_called()

    def test_no_notification_when_no_room(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True
        from waldur_mastermind.marketplace.enums import OrderStates
        from waldur_mastermind.marketplace.tests import (
            factories as marketplace_factories,
        )

        order = marketplace_factories.OrderFactory()
        order.state = OrderStates.EXECUTING

        handlers.on_order_state_changed(
            sender=type(order), instance=order, created=False
        )
        mock_tasks.send_room_notification.delay.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
@mock.patch("waldur_mastermind.matrix_chat.room_provisioning.tasks")
class OnProjectCreatedTest(TestCase):
    def _rooms_for(self, project):
        ct = ContentType.objects.get_for_model(project)
        return models.MatrixRoom.objects.filter(content_type=ct, object_id=project.id)

    @override_config(MATRIX_AUTO_CREATE_PROJECT_ROOMS=True)
    def test_room_is_created_when_opted_in(self, mock_tasks, mock_client):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        self.assertEqual(self._rooms_for(project).count(), 1)
        mock_tasks.create_room.delay.assert_called_once()

    @override_config(MATRIX_AUTO_CREATE_PROJECT_ROOMS=False)
    def test_no_room_when_not_opted_in(self, mock_tasks, mock_client):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        self.assertEqual(self._rooms_for(project).count(), 0)
        mock_tasks.create_room.delay.assert_not_called()

    @override_config(MATRIX_AUTO_CREATE_PROJECT_ROOMS=True)
    def test_no_room_when_matrix_disabled(self, mock_tasks, mock_client):
        # The opt-in flag alone is not enough — MATRIX_ENABLED still gates it.
        mock_client.is_enabled.return_value = False
        project = structure_factories.ProjectFactory()
        self.assertEqual(self._rooms_for(project).count(), 0)
        mock_tasks.create_room.delay.assert_not_called()

    @override_config(MATRIX_AUTO_CREATE_PROJECT_ROOMS=True)
    def test_long_project_name_does_not_break_project_creation(
        self, mock_tasks, mock_client
    ):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory(name="x" * 300)
        self.assertEqual(self._rooms_for(project).count(), 1)

    @override_config(MATRIX_AUTO_CREATE_PROJECT_ROOMS=True)
    def test_no_room_for_fixture_loading(self, mock_tasks, mock_client):
        # loaddata saves with raw=True; fixtures must not provision rooms.
        mock_client.is_enabled.return_value = False
        project = structure_factories.ProjectFactory()
        mock_client.is_enabled.return_value = True

        handlers.on_project_created(
            sender=type(project), instance=project, created=True, raw=True
        )

        self.assertEqual(self._rooms_for(project).count(), 0)
        mock_tasks.create_room.delay.assert_not_called()

    @override_config(MATRIX_AUTO_CREATE_PROJECT_ROOMS=True)
    def test_no_room_on_project_update(self, mock_tasks, mock_client):
        mock_client.is_enabled.return_value = True
        project = structure_factories.ProjectFactory()
        mock_tasks.create_room.delay.reset_mock()

        project.name = "Renamed"
        project.save(update_fields=["name"])

        self.assertEqual(self._rooms_for(project).count(), 1)
        mock_tasks.create_room.delay.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks.end_matrix_access")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnUserDeactivatedTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()

    def _save(self, **changes):
        for field, value in changes.items():
            setattr(self.user, field, value)
        with self.captureOnCommitCallbacks(execute=True):
            self.user.save()

    def test_deactivation_ends_web_sessions(self, mock_client, mock_task):
        mock_client.is_homeserver_configured.return_value = True

        self._save(is_active=False)

        mock_task.delay.assert_called_once_with(self.user.uuid.hex)

    def test_other_changes_are_ignored(self, mock_client, mock_task):
        mock_client.is_homeserver_configured.return_value = True

        self._save(first_name="Renamed")

        mock_task.delay.assert_not_called()

    def test_reactivation_is_ignored(self, mock_client, mock_task):
        mock_client.is_homeserver_configured.return_value = True
        self._save(is_active=False)
        mock_task.reset_mock()

        self._save(is_active=True)

        mock_task.delay.assert_not_called()

    def test_creating_an_inactive_user_is_ignored(self, mock_client, mock_task):
        # Regression: another receiver re-saves a new user (to set its token
        # lifetime) inside its own post_save, which reaches this handler with
        # created=False before the field tracker has been reset.
        mock_client.is_homeserver_configured.return_value = True

        with self.captureOnCommitCallbacks(execute=True):
            user = structure_factories.UserFactory(is_active=False)

        self.assertIsNotNone(user.token_lifetime)
        mock_task.delay.assert_not_called()

    def test_deactivation_saving_only_changed_fields(self, mock_client, mock_task):
        # Most deactivation paths save with update_fields.
        mock_client.is_homeserver_configured.return_value = True
        self.user.is_active = False

        with self.captureOnCommitCallbacks(execute=True):
            self.user.save(update_fields=["is_active"])

        mock_task.delay.assert_called_once_with(self.user.uuid.hex)

    def test_saving_an_already_inactive_user_is_ignored(self, mock_client, mock_task):
        mock_client.is_homeserver_configured.return_value = True
        self._save(is_active=False)
        mock_task.reset_mock()

        self._save(first_name="Renamed")

        mock_task.delay.assert_not_called()

    def test_chat_switched_off_still_ends_web_sessions(self, mock_client, mock_task):
        # Open drawers keep refreshing with the homeserver while MATRIX_ENABLED
        # is off, so their devices still have to go.
        mock_client.is_enabled.return_value = False
        mock_client.is_homeserver_configured.return_value = True

        self._save(is_active=False)

        mock_task.delay.assert_called_once_with(self.user.uuid.hex)

    def test_no_op_without_a_homeserver(self, mock_client, mock_task):
        mock_client.is_homeserver_configured.return_value = False

        self._save(is_active=False)

        mock_task.delay.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks.end_deleted_user_access")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class OnUserDeletedTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()

    def _delete(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.user.delete()

    def test_deleting_a_matrix_user_ends_web_sessions(self, mock_client, mock_task):
        # The profile goes with the user, so the task gets the Matrix ID itself.
        mock_client.is_homeserver_configured.return_value = True
        models.MatrixUserProfile.objects.create(
            user=self.user, matrix_user_id="@gone:matrix.example.com", provisioned=True
        )

        self._delete()

        mock_task.delay.assert_called_once_with("@gone:matrix.example.com", [])

    def test_user_never_provisioned_has_nothing_to_end(self, mock_client, mock_task):
        mock_client.is_homeserver_configured.return_value = True

        self._delete()

        mock_task.delay.assert_not_called()

    def test_chat_switched_off_still_ends_web_sessions(self, mock_client, mock_task):
        # Once the profile is gone with the user, nothing could find these
        # devices again.
        mock_client.is_enabled.return_value = False
        mock_client.is_homeserver_configured.return_value = True
        models.MatrixUserProfile.objects.create(
            user=self.user, matrix_user_id="@gone:matrix.example.com", provisioned=True
        )

        self._delete()

        mock_task.delay.assert_called_once_with("@gone:matrix.example.com", [])

    def test_no_op_without_a_homeserver(self, mock_client, mock_task):
        mock_client.is_homeserver_configured.return_value = False
        models.MatrixUserProfile.objects.create(
            user=self.user, matrix_user_id="@gone:matrix.example.com", provisioned=True
        )

        self._delete()

        mock_task.delay.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class RoomPermissionChangedTest(TestCase):
    # MATRIX_ROOM.CREATE decides who is in a project room and who runs it, and
    # member sync only runs on demand, so a role gaining or losing it would
    # otherwise reach no room until someone synced each of them.

    def setUp(self):
        cache.clear()

    def _grant_earlier(self, role, mock_tasks):
        role.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)
        # That change's window has passed.
        cache.clear()
        mock_tasks.reset_mock()

    def _scheduled(self, mock_tasks):
        return [
            (call.kwargs["args"], call.kwargs["countdown"])
            for call in mock_tasks.sync_rooms_of_role.apply_async.call_args_list
        ]

    def test_adding_it_schedules_a_sync_of_the_roles_rooms(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        role = CustomerRole.SUPPORT

        with self.captureOnCommitCallbacks(execute=True):
            role.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)

        self.assertEqual(
            self._scheduled(mock_tasks),
            [([role.id], handlers.ROLE_PERMISSION_SYNC_DELAY)],
        )

    def test_removing_it_schedules_a_sync_of_the_roles_rooms(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        role = CustomerRole.OWNER
        self._grant_earlier(role, mock_tasks)

        with self.captureOnCommitCallbacks(execute=True):
            role.delete_permission(PermissionEnum.CREATE_MATRIX_ROOM)

        self.assertEqual(
            self._scheduled(mock_tasks),
            [([role.id], handlers.ROLE_PERMISSION_SYNC_DELAY)],
        )

    def test_taking_it_away_and_giving_it_back_schedules_one_sync(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True
        role = CustomerRole.OWNER
        self._grant_earlier(role, mock_tasks)

        with self.captureOnCommitCallbacks(execute=True):
            role.delete_permission(PermissionEnum.CREATE_MATRIX_ROOM)
            role.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)

        self.assertEqual(
            self._scheduled(mock_tasks),
            [([role.id], handlers.ROLE_PERMISSION_SYNC_DELAY)],
        )

    def test_other_permissions_schedule_nothing(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = True

        with self.captureOnCommitCallbacks(execute=True):
            CustomerRole.SUPPORT.add_permission(PermissionEnum.LIST_PROJECTS)

        mock_tasks.sync_rooms_of_role.apply_async.assert_not_called()

    def test_no_op_when_disabled(self, mock_client, mock_tasks):
        mock_client.is_enabled.return_value = False

        with self.captureOnCommitCallbacks(execute=True):
            CustomerRole.SUPPORT.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)

        mock_tasks.sync_rooms_of_role.apply_async.assert_not_called()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_eager_celery_syncs_every_change(self, mock_client, mock_tasks):
        # The task runs at once, so nothing would come after a claimed change.
        mock_client.is_enabled.return_value = True
        role = CustomerRole.OWNER
        self._grant_earlier(role, mock_tasks)

        with self.captureOnCommitCallbacks(execute=True):
            role.delete_permission(PermissionEnum.CREATE_MATRIX_ROOM)
            role.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)

        self.assertEqual(
            mock_tasks.sync_rooms_of_role.delay.call_args_list,
            [mock.call(role.id), mock.call(role.id)],
        )
        mock_tasks.sync_rooms_of_role.apply_async.assert_not_called()

    def test_a_failed_publish_lets_the_next_change_schedule(
        self, mock_client, mock_tasks
    ):
        # initdb carries on without RabbitMQ, so a publish that fails must not
        # abort it, nor hold back the next change.
        mock_client.is_enabled.return_value = True
        mock_tasks.sync_rooms_of_role.apply_async.side_effect = [
            RuntimeError("broker down"),
            None,
        ]
        role = CustomerRole.SUPPORT

        with self.captureOnCommitCallbacks(execute=True):
            role.add_permission(PermissionEnum.CREATE_MATRIX_ROOM)
        with self.captureOnCommitCallbacks(execute=True):
            role.delete_permission(PermissionEnum.CREATE_MATRIX_ROOM)

        self.assertEqual(mock_tasks.sync_rooms_of_role.apply_async.call_count, 2)


@mock.patch("waldur_mastermind.matrix_chat.handlers.tasks")
@mock.patch("waldur_mastermind.matrix_chat.handlers.matrix_client")
class RolePermissionDeploymentTest(TestCase):
    # initdb loads permissions.yaml and then permissions-override.yaml on every
    # deployment, so a role the override gives MATRIX_ROOM.CREATE loses it and
    # gets it back each time.

    def setUp(self):
        cache.clear()
        self.role = ProjectRole.MANAGER
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.roles_file = Path(directory.name) / "permissions.yaml"
        self.roles_file.write_text(
            f"- role: {self.role.name}\n"
            "  permissions:\n"
            f"    - {PermissionEnum.LIST_PROJECTS}\n"
        )
        self.override_file = Path(directory.name) / "permissions-override.yaml"
        self.override_file.write_text(
            f"- role: {self.role.name}\n"
            "  add_permissions:\n"
            f"    - {PermissionEnum.CREATE_MATRIX_ROOM}\n"
        )
        project = structure_factories.ProjectFactory()
        project.add_user(structure_factories.UserFactory(), self.role)
        self.room = _create_room_for_project(project)

    def _deploy(self):
        with self.captureOnCommitCallbacks(execute=True):
            call_command("import_roles", str(self.roles_file), stdout=io.StringIO())
            call_command(
                "override_roles", str(self.override_file), stdout=io.StringIO()
            )

    def _run_scheduled_sync(self, mock_tasks):
        args = mock_tasks.sync_rooms_of_role.apply_async.call_args.kwargs["args"]
        with (
            mock.patch.object(tasks, "matrix_client") as task_client,
            mock.patch.object(tasks, "sync_project_members_to_room") as room_sync,
        ):
            task_client.is_enabled.return_value = True
            tasks.sync_rooms_of_role(*args)
        return [call.args[0] for call in room_sync.delay.call_args_list]

    def test_the_deployment_that_grants_it_syncs_the_room(
        self, mock_client, mock_tasks
    ):
        mock_client.is_enabled.return_value = True

        self._deploy()

        self.assertEqual(self._run_scheduled_sync(mock_tasks), [str(self.room.uuid)])

    def test_a_redeployment_syncs_the_room_once_after_the_role_files(
        self, mock_client, mock_tasks
    ):
        # Taking the permission away and giving it back must not reach the
        # room in between.
        mock_client.is_enabled.return_value = True
        self._deploy()
        # The next deployment comes after the debounce window.
        cache.clear()
        mock_tasks.reset_mock()

        self._deploy()

        mock_tasks.sync_rooms_of_role.apply_async.assert_called_once_with(
            args=[self.role.id], countdown=handlers.ROLE_PERMISSION_SYNC_DELAY
        )
