import json
from unittest import mock

import httpx
import respx
from constance.test import override_config
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import matrix_client, models, tasks

HOMESERVER = "https://matrix.example.com"
ALICE = "@alice:matrix.example.com"
ADMIN_USER = f"{HOMESERVER}/_synapse/admin/v2/users/%40alice%3Amatrix.example.com"
MATRIX = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)


def _user(is_active, username="alice"):
    user = structure_factories.UserFactory(username=username, is_active=is_active)
    models.MatrixUserProfile.objects.create(
        user=user, matrix_user_id=f"@{username}:matrix.example.com", provisioned=True
    )
    return user


def _set_active(user, is_active):
    type(user).all_objects.filter(pk=user.pk).update(is_active=is_active)


def _homeserver(mock_client):
    """Leave the client mocked, but with the exceptions the tasks catch."""
    mock_client.is_homeserver_configured.return_value = True
    mock_client.MatrixClientError = matrix_client.MatrixClientError
    mock_client.MatrixAdminRequired = matrix_client.MatrixAdminRequired
    mock_client.MatrixUserLocked = matrix_client.MatrixUserLocked
    mock_client.MatrixUserNotFound = matrix_client.MatrixUserNotFound
    mock_client.MatrixAccountIsHomeserverAdmin = (
        matrix_client.MatrixAccountIsHomeserverAdmin
    )


def _admin_flag(admin):
    """The admin API's answer about the account, asked before a lock."""
    return respx.get(ADMIN_USER).mock(
        return_value=httpx.Response(200, json={"admin": admin, "locked": False})
    )


@override_config(**MATRIX)
class SetLockedTest(TestCase):
    @respx.mock
    def test_a_homeserver_admin_is_never_locked(self):
        # A user was linked to the admin's account before that was refused;
        # deactivating them in Waldur must not lock the admin out.
        _admin_flag(True)
        route = respx.put(ADMIN_USER).mock(return_value=httpx.Response(200, json={}))

        with self.assertLogs("waldur_mastermind.matrix_chat.matrix_client", "WARNING"):
            self.assertFalse(matrix_client.set_locked(ALICE, True))

        self.assertFalse(route.called)

    @respx.mock
    def test_a_homeserver_admin_can_be_unlocked(self):
        # Not asked about: a lock placed before the check has to come off.
        check = _admin_flag(True)
        route = respx.put(ADMIN_USER).mock(return_value=httpx.Response(200, json={}))

        self.assertTrue(matrix_client.set_locked(ALICE, False))

        self.assertTrue(route.called)
        self.assertFalse(check.called)

    @respx.mock
    def test_no_lock_while_the_homeserver_fails_to_say_who_is_an_admin(self):
        # The lock that follows could succeed, on an admin's account.
        route = respx.put(ADMIN_USER).mock(return_value=httpx.Response(200, json={}))
        for answer in (
            httpx.Response(502, text="Bad Gateway"),
            httpx.Response(200, json={"name": ALICE}),
            httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
        ):
            respx.get(ADMIN_USER).mock(return_value=answer)

            with self.assertRaises(matrix_client.MatrixClientError) as cm:
                matrix_client.set_locked(ALICE, True)

            # Worth a retry, unlike a bot without admin rights.
            self.assertNotIsInstance(cm.exception, matrix_client.MatrixAdminRequired)
        self.assertFalse(route.called)

    @respx.mock
    def test_an_account_the_homeserver_does_not_have_is_told_apart(self):
        respx.get(ADMIN_USER).mock(
            return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
        )
        route = respx.put(ADMIN_USER).mock(return_value=httpx.Response(200, json={}))

        with self.assertRaises(matrix_client.MatrixUserNotFound):
            matrix_client.set_locked(ALICE, True)

        self.assertFalse(route.called)

    @respx.mock
    def test_locks_through_the_admin_api_as_the_bot(self):
        check = _admin_flag(False)
        route = respx.put(ADMIN_USER).mock(return_value=httpx.Response(200, json={}))

        self.assertTrue(matrix_client.set_locked(ALICE, True))

        self.assertTrue(check.called)
        request = route.calls.last.request
        self.assertEqual(request.headers["Authorization"], "Bearer test-as-token")
        self.assertEqual(json.loads(request.content), {"locked": True})

    @respx.mock
    def test_unlocks(self):
        route = respx.put(ADMIN_USER).mock(return_value=httpx.Response(200, json={}))

        matrix_client.set_locked(ALICE, False)

        self.assertEqual(
            json.loads(route.calls.last.request.content), {"locked": False}
        )

    @respx.mock
    def test_a_matrix_id_with_a_slash_stays_one_path_segment(self):
        respx.get(url__startswith=f"{HOMESERVER}/_synapse/admin/v2/users/").mock(
            return_value=httpx.Response(200, json={"admin": False})
        )
        route = respx.put(
            url__startswith=f"{HOMESERVER}/_synapse/admin/v2/users/"
        ).mock(return_value=httpx.Response(200, json={}))

        matrix_client.set_locked("@a/../b:matrix.example.com", True)

        for call in respx.calls:
            self.assertEqual(
                call.request.url.raw_path,
                b"/_synapse/admin/v2/users/%40a%2F..%2Fb%3Amatrix.example.com",
            )
        self.assertTrue(route.called)

    @respx.mock
    def test_a_bot_that_is_not_an_admin_is_told_apart(self):
        # Refused at the question already, so the lock is not tried.
        respx.get(ADMIN_USER).mock(
            return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
        )
        route = respx.put(ADMIN_USER).mock(return_value=httpx.Response(200, json={}))

        with self.assertRaises(matrix_client.MatrixAdminRequired):
            matrix_client.set_locked(ALICE, True)

        self.assertFalse(route.called)

    @respx.mock
    def test_a_lock_the_homeserver_refuses_to_a_bot_that_is_no_admin(self):
        # The bot lost its admin rights between the two calls.
        _admin_flag(False)
        respx.put(ADMIN_USER).mock(
            return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
        )

        with self.assertRaises(matrix_client.MatrixAdminRequired):
            matrix_client.set_locked(ALICE, True)

    @respx.mock
    def test_other_failures_are_client_errors(self):
        _admin_flag(False)
        respx.put(ADMIN_USER).mock(return_value=httpx.Response(502, text="Bad Gateway"))

        with self.assertRaises(matrix_client.MatrixClientError) as cm:
            matrix_client.set_locked(ALICE, True)
        self.assertNotIsInstance(cm.exception, matrix_client.MatrixAdminRequired)

    @respx.mock
    def test_a_homeserver_without_the_admin_api_is_told_apart_too(self):
        # Retrying cannot help.
        unrecognised = httpx.Response(404, json={"errcode": "M_UNRECOGNIZED"})
        respx.get(ADMIN_USER).mock(return_value=unrecognised)
        respx.put(ADMIN_USER).mock(return_value=unrecognised)

        for locked in (True, False):
            with self.assertRaises(matrix_client.MatrixAdminRequired):
                matrix_client.set_locked(ALICE, locked)

    @respx.mock
    def test_the_bot_is_never_locked(self):
        # A stored Matrix ID can map onto the bot, and a locked bot stops chat
        # for everyone.
        route = respx.route(
            url__startswith=f"{HOMESERVER}/_synapse/admin/v2/users/"
        ).mock(return_value=httpx.Response(200, json={}))

        self.assertFalse(
            matrix_client.set_locked("@waldur-bot:matrix.example.com", True)
        )

        self.assertFalse(route.called)


@override_config(**MATRIX)
class LockedAccountTest(TestCase):
    def setUp(self):
        # A web session first asks whether the account is a homeserver admin's.
        patcher = mock.patch.object(
            matrix_client, "is_homeserver_admin", return_value=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @respx.mock
    def test_a_login_refused_for_a_locked_account_is_told_apart(self):
        # Trying again later cannot help: Waldur unlocks only at reactivation.
        respx.post(f"{HOMESERVER}/_matrix/client/v3/login").mock(
            return_value=httpx.Response(401, json={"errcode": "M_USER_LOCKED"})
        )

        with self.assertRaises(matrix_client.MatrixUserLocked):
            matrix_client.create_web_session(ALICE)

    @respx.mock
    def test_a_device_listing_refused_for_a_locked_account_is_told_apart(self):
        respx.get(f"{HOMESERVER}/_matrix/client/v3/devices").mock(
            return_value=httpx.Response(401, json={"errcode": "M_USER_LOCKED"})
        )

        with self.assertRaises(matrix_client.MatrixUserLocked):
            matrix_client.logout_all_devices(ALICE)

    @respx.mock
    def test_an_account_locked_between_the_listing_and_a_sign_out_is_told_apart(self):
        # Not folded into "could not sign out devices", which is retried.
        respx.get(f"{HOMESERVER}/_matrix/client/v3/devices").mock(
            return_value=httpx.Response(200, json={"devices": [{"device_id": "D1"}]})
        )
        respx.post(f"{HOMESERVER}/_matrix/client/v3/login").mock(
            return_value=httpx.Response(401, json={"errcode": "M_USER_LOCKED"})
        )

        with self.assertRaises(matrix_client.MatrixUserLocked):
            matrix_client.logout_all_devices(ALICE)

    @respx.mock
    def test_other_refused_logins_are_client_errors(self):
        respx.post(f"{HOMESERVER}/_matrix/client/v3/login").mock(
            return_value=httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
        )

        with self.assertRaises(matrix_client.MatrixClientError) as cm:
            matrix_client.create_web_session(ALICE)
        self.assertNotIsInstance(cm.exception, matrix_client.MatrixUserLocked)


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class LockOnDeactivationTest(TestCase):
    def test_locks_after_the_sign_out_and_the_kicks(self, mock_client):
        # A locked account refuses the appservice, which signs the devices out.
        _homeserver(mock_client)
        user = _user(is_active=False)
        project = structure_factories.ProjectFactory()
        room = models.MatrixRoom.objects.create(
            room_id="!room:matrix.example.com",
            room_name=project.name,
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
        )
        models.MatrixRoomMember.objects.create(
            room=room,
            user=user,
            matrix_user_id=ALICE,
            membership_state=models.MembershipStates.JOINED,
        )

        tasks.end_matrix_access(user.uuid.hex)

        self.assertEqual(
            [
                c[0]
                for c in mock_client.mock_calls
                if c[0] in ("logout_all_devices", "kick_user", "set_locked")
            ],
            ["logout_all_devices", "kick_user", "set_locked"],
        )
        mock_client.set_locked.assert_called_once_with(ALICE, True)

    def test_locks_while_chat_is_switched_off(self, mock_client):
        _homeserver(mock_client)
        mock_client.is_enabled.return_value = False
        user = _user(is_active=False)

        tasks.end_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_called_once_with(ALICE, True)

    def test_a_bot_without_admin_rights_is_not_retried(self, mock_client):
        # Retrying cannot help until an operator makes the bot an admin; the
        # sign-out and the kicks are done all the same.
        _homeserver(mock_client)
        mock_client.set_locked.side_effect = matrix_client.MatrixAdminRequired("403")
        user = _user(is_active=False)

        with self.assertLogs("waldur_mastermind.matrix_chat.tasks", "WARNING"):
            tasks.end_matrix_access(user.uuid.hex)

        mock_client.logout_all_devices.assert_called_once_with(ALICE)

    def test_a_lock_the_homeserver_fails_is_retried(self, mock_client):
        _homeserver(mock_client)
        mock_client.set_locked.side_effect = matrix_client.MatrixClientError("502")
        user = _user(is_active=False)

        with self.assertRaises(matrix_client.MatrixClientError):
            tasks.end_matrix_access(user.uuid.hex)

    def test_a_failed_sign_out_still_locks_and_is_not_retried(self, mock_client):
        # Locked, the devices left behind are refused like any other, and a
        # retry could not sign them out: the appservice is refused too.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "503"
        )
        user = _user(is_active=False)

        with self.assertLogs("waldur_mastermind.matrix_chat.tasks", "WARNING") as cm:
            tasks.end_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_called_once_with(ALICE, True)
        self.assertIn("could not be signed out", cm.output[0])

    def test_a_failed_sign_out_is_retried_while_the_account_is_not_locked(
        self, mock_client
    ):
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "503"
        )
        mock_client.set_locked.side_effect = matrix_client.MatrixAdminRequired("403")
        user = _user(is_active=False)

        with self.assertRaisesRegex(matrix_client.MatrixClientError, "503"):
            tasks.end_matrix_access(user.uuid.hex)

    def test_a_failed_sign_out_is_retried_for_an_account_that_is_not_locked(
        self, mock_client
    ):
        # set_locked answers False for a homeserver admin's account: nothing
        # was locked, so the log must not say so and the sign-out is retried.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "503"
        )
        mock_client.set_locked.return_value = False
        user = _user(is_active=False)

        with self.assertNoLogs("waldur_mastermind.matrix_chat.tasks", "WARNING"):
            with self.assertRaisesRegex(matrix_client.MatrixClientError, "503"):
                tasks.end_matrix_access(user.uuid.hex)

    def test_an_account_the_homeserver_does_not_have_is_not_retried(self, mock_client):
        # A profile kept through a wiped homeserver: nothing to lock, and no
        # device that a retry of the failed sign-out could reach.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "403"
        )
        mock_client.set_locked.side_effect = matrix_client.MatrixUserNotFound("404")
        user = _user(is_active=False)

        tasks.end_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_called_once_with(ALICE, True)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.restore_matrix_access")
    def test_a_user_reactivated_while_it_ran_gets_their_access_restored(
        self, mock_restore, mock_client
    ):
        # The reactivation's unlock and room syncs may have run before this
        # lock and these kicks, and nothing would undo them.
        _homeserver(mock_client)
        user = _user(is_active=False)

        def reactivated_meanwhile(matrix_user_id, locked):
            _set_active(user, True)
            return True

        mock_client.set_locked.side_effect = reactivated_meanwhile

        tasks.end_matrix_access(user.uuid.hex)

        mock_restore.delay.assert_called_once_with(user.uuid.hex)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.restore_matrix_access")
    def test_a_reactivated_user_is_restored_without_a_lock_too(
        self, mock_restore, mock_client
    ):
        # The kicks happen whether or not the bot can lock.
        _homeserver(mock_client)
        user = _user(is_active=False)

        def reactivated_meanwhile(matrix_user_id, locked):
            _set_active(user, True)
            raise matrix_client.MatrixAdminRequired("403")

        mock_client.set_locked.side_effect = reactivated_meanwhile

        tasks.end_matrix_access(user.uuid.hex)

        mock_restore.delay.assert_called_once_with(user.uuid.hex)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.restore_matrix_access")
    def test_restoring_takes_over_from_a_retry(self, mock_restore, mock_client):
        # A retry would find the user active and do nothing.
        _homeserver(mock_client)
        user = _user(is_active=False)

        def reactivated_meanwhile(matrix_user_id, locked):
            _set_active(user, True)
            raise matrix_client.MatrixClientError("502")

        mock_client.set_locked.side_effect = reactivated_meanwhile

        tasks.end_matrix_access(user.uuid.hex)

        mock_restore.delay.assert_called_once_with(user.uuid.hex)

    @mock.patch("waldur_mastermind.matrix_chat.tasks.restore_matrix_access")
    def test_a_user_who_stays_deactivated_is_not_restored(
        self, mock_restore, mock_client
    ):
        _homeserver(mock_client)
        user = _user(is_active=False)

        tasks.end_matrix_access(user.uuid.hex)

        mock_restore.delay.assert_not_called()

    def test_an_account_locked_already_is_left_as_it_is(self, mock_client):
        # A second run: the account refuses the sign-out as it refuses its
        # devices, which is no failure, and there is nothing left to lock. So
        # nothing warns and nothing is retried, whether or not the admin API
        # is usable by now.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixUserLocked(
            "401"
        )
        mock_client.set_locked.side_effect = matrix_client.MatrixAdminRequired("404")
        user = _user(is_active=False)

        with self.assertNoLogs("waldur_mastermind.matrix_chat.tasks", "WARNING"):
            tasks.end_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_not_called()

    def test_a_user_reactivated_before_it_ran_is_not_locked(self, mock_client):
        _homeserver(mock_client)
        user = _user(is_active=True)

        tasks.end_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_not_called()

    def test_a_user_never_provisioned_has_no_account_to_lock(self, mock_client):
        _homeserver(mock_client)
        user = structure_factories.UserFactory(is_active=False)

        tasks.end_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class LockOnDeletionTest(TestCase):
    def _order(self, mock_client):
        steps = ("kick_user", "logout_all_devices", "set_password", "set_locked")
        return [c[0] for c in mock_client.mock_calls if c[0] in steps]

    def test_replaces_the_password_and_locks_after_the_kicks_and_the_sign_out(
        self, mock_client
    ):
        _homeserver(mock_client)

        tasks.end_deleted_user_access(ALICE, ["!a:x"])

        self.assertEqual(
            self._order(mock_client),
            ["kick_user", "logout_all_devices", "set_password", "set_locked"],
        )
        mock_client.set_locked.assert_called_once_with(ALICE, True)

    def test_the_password_is_replaced_with_one_nobody_knows(self, mock_client):
        # The user may have generated it and still know it, and the account
        # may be unlocked one day, for a new user who gets this Matrix ID.
        _homeserver(mock_client)
        mock_client.new_password.side_effect = matrix_client.new_password

        tasks.end_deleted_user_access(ALICE)

        (matrix_user_id, password), kwargs = mock_client.set_password.call_args
        self.assertEqual(matrix_user_id, ALICE)
        self.assertGreaterEqual(len(password), 32)
        self.assertEqual(kwargs, {"logout_devices": True})

    def test_the_admin_api_signs_out_what_the_sign_out_could_not(self, mock_client):
        # So a failed sign-out is neither retried nor reported.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "503"
        )

        with self.assertNoLogs("waldur_mastermind.matrix_chat.tasks", "WARNING"):
            tasks.end_deleted_user_access(ALICE)

        mock_client.set_locked.assert_called_once_with(ALICE, True)

    def test_a_user_deactivated_before_is_locked_already(self, mock_client):
        # The usual order. The password is still replaced, and nothing warns.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixUserLocked(
            "401"
        )

        with self.assertNoLogs("waldur_mastermind.matrix_chat.tasks", "WARNING"):
            tasks.end_deleted_user_access(ALICE)

        mock_client.set_password.assert_called_once()
        mock_client.set_locked.assert_not_called()

    def test_a_bot_without_admin_rights_is_not_retried(self, mock_client):
        # One warning says what was not done; the lock goes through the same
        # API and is not tried.
        _homeserver(mock_client)
        mock_client.set_password.side_effect = matrix_client.MatrixAdminRequired("403")

        with self.assertLogs("waldur_mastermind.matrix_chat.tasks", "WARNING") as cm:
            tasks.end_deleted_user_access(ALICE)

        self.assertEqual(len(cm.output), 1)
        self.assertIn("not replaced", cm.output[0])
        self.assertIn("left unlocked", cm.output[0])
        mock_client.logout_all_devices.assert_called_once_with(ALICE)
        mock_client.set_locked.assert_not_called()

    def test_the_warning_does_not_call_a_locked_account_unlocked(self, mock_client):
        # Locked at deactivation; the admin API stopped being usable since.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixUserLocked(
            "401"
        )
        mock_client.set_password.side_effect = matrix_client.MatrixAdminRequired("403")

        with self.assertLogs("waldur_mastermind.matrix_chat.tasks", "WARNING") as cm:
            tasks.end_deleted_user_access(ALICE)

        self.assertEqual(len(cm.output), 1)
        self.assertIn("not replaced", cm.output[0])
        self.assertIn("locked already", cm.output[0])

    def test_an_account_the_homeserver_does_not_have_ends_it(self, mock_client):
        # A profile kept through a move to another homeserver. Nothing can
        # succeed later, so nothing is retried, and no account is locked into
        # existence.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "403"
        )
        mock_client.set_password.side_effect = matrix_client.MatrixUserNotFound("404")

        tasks.end_deleted_user_access(ALICE)

        mock_client.set_locked.assert_not_called()

    def test_a_homeserver_admins_account_is_left_as_it_is(self, mock_client):
        # The deleted user was linked to it before that was refused. The admin
        # keeps the password and is not locked out, and nothing is retried.
        _homeserver(mock_client)
        mock_client.set_password.side_effect = (
            matrix_client.MatrixAccountIsHomeserverAdmin("@alice is a homeserver admin")
        )

        with self.assertLogs("waldur_mastermind.matrix_chat.tasks", "WARNING") as cm:
            tasks.end_deleted_user_access(ALICE)

        # One warning, which says what was skipped rather than "left as it
        # is": the devices were signed out and the rooms left.
        self.assertEqual(len(cm.output), 1)
        self.assertIn("not replaced", cm.output[0])
        self.assertIn("not locked", cm.output[0])
        mock_client.set_locked.assert_not_called()

    def test_a_homeserver_admins_failed_sign_out_is_retried(self, mock_client):
        # No lock stands in for the sign-out here, so it has to succeed.
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "503"
        )
        mock_client.set_password.side_effect = (
            matrix_client.MatrixAccountIsHomeserverAdmin("@alice is a homeserver admin")
        )

        with self.assertRaisesRegex(matrix_client.MatrixClientError, "503"):
            tasks.end_deleted_user_access(ALICE)

        mock_client.set_locked.assert_not_called()

    def test_nothing_is_done_to_the_bot(self, mock_client):
        # A stored Matrix ID can map onto the bot.
        _homeserver(mock_client)
        mock_client.get_bot_user_id.return_value = ALICE

        with self.assertLogs("waldur_mastermind.matrix_chat.tasks", "WARNING"):
            tasks.end_deleted_user_access(ALICE, ["!a:x"])

        mock_client.kick_user.assert_not_called()
        mock_client.set_password.assert_not_called()
        mock_client.set_locked.assert_not_called()

    def test_a_failed_sign_out_is_retried_while_the_admin_api_is_not_usable(
        self, mock_client
    ):
        _homeserver(mock_client)
        mock_client.logout_all_devices.side_effect = matrix_client.MatrixClientError(
            "503"
        )
        mock_client.set_password.side_effect = matrix_client.MatrixAdminRequired("403")

        with self.assertRaisesRegex(matrix_client.MatrixClientError, "503"):
            tasks.end_deleted_user_access(ALICE)

    def test_a_password_or_a_lock_the_homeserver_fails_is_retried(self, mock_client):
        for failing in ("set_password", "set_locked"):
            with self.subTest(failing=failing):
                mock_client.reset_mock(side_effect=True)
                _homeserver(mock_client)
                getattr(
                    mock_client, failing
                ).side_effect = matrix_client.MatrixClientError("502")

                with self.assertRaisesRegex(matrix_client.MatrixClientError, "502"):
                    tasks.end_deleted_user_access(ALICE)

                # A password that could not be replaced does not hold up the lock.
                mock_client.set_locked.assert_called_once_with(ALICE, True)


@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class PruneLockedAccountTest(TestCase):
    def test_the_device_prune_leaves_a_locked_account_alone(self, mock_client):
        # It lists the devices as the user, which a locked account refuses:
        # without this the daily prune would fail for the user every night.
        # Still marked, so the prune after an unlock finds what is left.
        _homeserver(mock_client)
        mock_client.list_web_devices.side_effect = matrix_client.MatrixUserLocked("401")

        tasks.prune_web_devices(ALICE)

        mock_client.clear_web_session_mark.assert_not_called()


@mock.patch("waldur_mastermind.matrix_chat.tasks.sync_project_members_to_room")
@mock.patch("waldur_mastermind.matrix_chat.tasks.matrix_client")
class RestoreMatrixAccessTest(TestCase):
    def _room_of(self, user):
        project = structure_factories.ProjectFactory()
        project.add_user(user, ProjectRole.MEMBER)
        return models.MatrixRoom.objects.create(
            room_id="!room:matrix.example.com",
            room_name=project.name,
            state=models.RoomStates.ACTIVE,
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
        )

    def test_unlocks_before_syncing_the_users_rooms(self, mock_client, mock_sync):
        # The syncs join rooms as the user, which a locked account refuses.
        _homeserver(mock_client)
        user = _user(is_active=True)
        room = self._room_of(user)
        order = mock.Mock()
        order.attach_mock(mock_client.set_locked, "set_locked")
        order.attach_mock(mock_sync.delay, "sync")

        tasks.restore_matrix_access(user.uuid.hex)

        self.assertEqual(
            order.mock_calls,
            [mock.call.set_locked(ALICE, False), mock.call.sync(str(room.uuid))],
        )

    def test_unlocks_while_chat_is_switched_off(self, mock_client, mock_sync):
        # Deactivation locks then too. Nobody is synced into rooms meanwhile.
        _homeserver(mock_client)
        mock_client.is_enabled.return_value = False
        user = _user(is_active=True)
        self._room_of(user)

        tasks.restore_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_called_once_with(ALICE, False)
        mock_sync.delay.assert_not_called()

    def test_skips_without_a_homeserver(self, mock_client, mock_sync):
        mock_client.is_homeserver_configured.return_value = False
        user = _user(is_active=True)

        tasks.restore_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_not_called()

    def test_a_user_deactivated_again_before_it_ran_stays_locked(
        self, mock_client, mock_sync
    ):
        _homeserver(mock_client)
        user = _user(is_active=False)
        self._room_of(user)

        tasks.restore_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_not_called()
        mock_sync.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.tasks.end_matrix_access")
    def test_a_user_deactivated_again_while_it_ran_has_their_access_ended(
        self, mock_end, mock_client, mock_sync
    ):
        # The deactivation's lock may have run before this unlock, and nothing
        # would lock the account again.
        _homeserver(mock_client)
        user = _user(is_active=True)
        self._room_of(user)

        def deactivated_meanwhile(matrix_user_id, locked):
            _set_active(user, False)
            return True

        mock_client.set_locked.side_effect = deactivated_meanwhile

        tasks.restore_matrix_access(user.uuid.hex)

        mock_end.delay.assert_called_once_with(user.uuid.hex)
        mock_sync.delay.assert_not_called()

    @mock.patch("waldur_mastermind.matrix_chat.tasks.end_matrix_access")
    def test_ending_access_takes_over_from_a_retry(
        self, mock_end, mock_client, mock_sync
    ):
        # An unlock the homeserver applied without answering: a retry would
        # find the user inactive and do nothing.
        _homeserver(mock_client)
        user = _user(is_active=True)
        self._room_of(user)

        def deactivated_meanwhile(matrix_user_id, locked):
            _set_active(user, False)
            raise matrix_client.MatrixClientError("timed out")

        mock_client.set_locked.side_effect = deactivated_meanwhile

        tasks.restore_matrix_access(user.uuid.hex)

        mock_end.delay.assert_called_once_with(user.uuid.hex)
        mock_sync.delay.assert_not_called()

    def test_a_bot_without_admin_rights_still_syncs_the_rooms(
        self, mock_client, mock_sync
    ):
        # Retrying the unlock cannot help, and usually the account was never
        # locked: the lock goes through the same API.
        _homeserver(mock_client)
        mock_client.set_locked.side_effect = matrix_client.MatrixAdminRequired("403")
        user = _user(is_active=True)
        self._room_of(user)

        with self.assertLogs("waldur_mastermind.matrix_chat.tasks", "WARNING"):
            tasks.restore_matrix_access(user.uuid.hex)

        mock_sync.delay.assert_called_once()

    def test_an_unlock_the_homeserver_fails_is_retried_before_any_sync(
        self, mock_client, mock_sync
    ):
        _homeserver(mock_client)
        mock_client.set_locked.side_effect = matrix_client.MatrixClientError("502")
        user = _user(is_active=True)
        self._room_of(user)

        with self.assertRaises(matrix_client.MatrixClientError):
            tasks.restore_matrix_access(user.uuid.hex)

        mock_sync.delay.assert_not_called()

    def test_a_user_never_provisioned_only_gets_the_room_syncs(
        self, mock_client, mock_sync
    ):
        _homeserver(mock_client)
        user = structure_factories.UserFactory()
        self._room_of(user)

        tasks.restore_matrix_access(user.uuid.hex)

        mock_client.set_locked.assert_not_called()
        mock_sync.delay.assert_called_once()
