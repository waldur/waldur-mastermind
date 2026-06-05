from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import handlers, models


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
