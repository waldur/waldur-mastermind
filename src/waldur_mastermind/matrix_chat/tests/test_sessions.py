import json
from unittest import mock

import httpx
from constance.test import override_config
from django.conf import settings
from django.forms.models import model_to_dict
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import models, views
from waldur_mastermind.matrix_chat.matrix_client import (
    MatrixClientError,
    MatrixUserLocked,
)
from waldur_mastermind.matrix_chat.tests import fixtures

SESSION = {
    "device_id": "WALDUR_WEB_0A1B2C3D4E5F",
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "expires_in_ms": 300000,
}


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="http://tuwunel.internal:6167",
    MATRIX_HOMESERVER_PUBLIC_URL="https://chat.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
@mock.patch("waldur_mastermind.matrix_chat.views.tasks.prune_web_devices")
@mock.patch(
    "waldur_mastermind.matrix_chat.matrix_client.create_web_session",
    return_value=SESSION,
)
@mock.patch(
    "waldur_mastermind.matrix_chat.matrix_client.ensure_user_exists",
    return_value="@alice:example.com",
)
class MatrixSessionTest(test.APITestCase):
    url = "/api/matrix/session/"

    def setUp(self):
        self.user = structure_factories.UserFactory()

    def test_anonymous_is_rejected(self, mock_ensure, mock_session, mock_prune):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        mock_session.assert_not_called()

    def test_returns_session_on_its_own_device(
        self, mock_ensure, mock_session, mock_prune
    ):
        self.client.force_authenticate(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "homeserver_url": "https://chat.example.com",
                "matrix_user_id": "@alice:example.com",
                **SESSION,
            },
        )
        mock_ensure.assert_called_once_with(self.user)
        mock_session.assert_called_once_with("@alice:example.com")

    def test_prunes_stale_web_devices(self, mock_ensure, mock_session, mock_prune):
        self.client.force_authenticate(self.user)

        self.client.post(self.url)

        mock_prune.delay.assert_called_once_with(
            "@alice:example.com", SESSION["device_id"]
        )

    def test_tokens_are_not_stored(self, mock_ensure, mock_session, mock_prune):
        profile = models.MatrixUserProfile.objects.create(
            user=self.user, matrix_user_id="@alice:example.com", provisioned=True
        )
        self.client.force_authenticate(self.user)

        self.client.post(self.url)

        profile.refresh_from_db()
        stored = model_to_dict(profile).values()
        self.assertNotIn(SESSION["access_token"], stored)
        self.assertNotIn(SESSION["refresh_token"], stored)

    def test_homeserver_error_is_unavailable(
        self, mock_ensure, mock_session, mock_prune
    ):
        mock_session.side_effect = MatrixClientError("M_FORBIDDEN internal detail")
        self.client.force_authenticate(self.user)

        response = self.client.post(self.url)

        # An upstream fault the drawer should retry, without homeserver text.
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn("internal detail", str(response.data))
        mock_prune.delay.assert_not_called()

    def test_a_locked_account_needs_an_administrator(
        self, mock_ensure, mock_session, mock_prune
    ):
        # Waldur unlocks an account only at reactivation, so "try again
        # later" would send the user round in circles.
        mock_session.side_effect = MatrixUserLocked("401 M_USER_LOCKED")
        self.client.force_authenticate(self.user)

        with self.assertLogs("waldur_mastermind.matrix_chat.views", "WARNING") as cm:
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(
            response.data["detail"],
            "Your chat account is locked; ask your administrator.",
        )
        self.assertIn("reactivate", cm.output[0])
        mock_prune.delay.assert_not_called()

    def test_an_account_locked_by_a_deactivation_just_now_is_forbidden(
        self, mock_ensure, mock_session, mock_prune
    ):
        # The deactivation's task was faster than this request.
        def locked_by_the_deactivation(matrix_user_id):
            type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
            raise MatrixUserLocked("401 M_USER_LOCKED")

        mock_session.side_effect = locked_by_the_deactivation
        self.client.force_authenticate(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_provisioning_error_is_unavailable(
        self, mock_ensure, mock_session, mock_prune
    ):
        mock_ensure.side_effect = MatrixClientError(
            "MATRIX_USER_REGISTRATION_SECRET must be configured"
        )
        self.client.force_authenticate(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn("MATRIX_USER_REGISTRATION_SECRET", str(response.data))

    def test_malformed_homeserver_reply_is_unavailable(
        self, mock_ensure, mock_session, mock_prune
    ):
        # A body that claims to be JSON but isn't, or isn't UTF-8 at all.
        self.client.force_authenticate(self.user)
        for error in [
            json.JSONDecodeError("Expecting value", "<html>", 0),
            UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte"),
        ]:
            with self.subTest(error=type(error).__name__):
                mock_ensure.side_effect = error

                response = self.client.post(self.url)

                self.assertEqual(
                    response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE
                )

    @mock.patch("waldur_mastermind.matrix_chat.views.tasks.end_matrix_access")
    @mock.patch("waldur_mastermind.matrix_chat.matrix_client.logout_device")
    def test_session_racing_a_deactivation_is_signed_out(
        self, mock_logout, mock_end, mock_ensure, mock_session, mock_prune
    ):
        # end_matrix_access may have listed the devices before this one existed,
        # or run before this request provisioned the account it locks.
        def deactivated_meanwhile(matrix_user_id):
            type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
            return SESSION

        mock_session.side_effect = deactivated_meanwhile
        self.client.force_authenticate(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_logout.assert_called_once_with("@alice:example.com", SESSION["device_id"])
        mock_end.delay.assert_called_once_with(self.user.uuid.hex)
        mock_prune.delay.assert_not_called()

    def test_hidden_when_matrix_is_disabled(
        self, mock_ensure, mock_session, mock_prune
    ):
        self.client.force_authenticate(self.user)

        with override_config(MATRIX_ENABLED=False):
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_ensure.assert_not_called()

    def test_has_its_own_throttle_scope(self, mock_ensure, mock_session, mock_prune):
        self.assertEqual(views.MatrixSessionView.throttle_scope, "matrix_session")
        self.assertIn(
            "matrix_session", settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        )


@override_config(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL="http://tuwunel.internal:6167",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
@mock.patch("waldur_mastermind.matrix_chat.matrix_client.join_room_as_user")
class MatrixRoomOpenTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MatrixChatFixture()
        self.room = self.fixture.matrix_room
        self.url = f"/api/matrix/rooms/{self.room.uuid.hex}/open/"

    def _member(self, user, state):
        return models.MatrixRoomMember.objects.create(
            room=self.room,
            user=user,
            matrix_user_id=f"@{user.username}:example.com",
            membership_state=state,
        )

    def test_joined_member_gets_room_id(self, mock_join):
        self._member(self.fixture.admin, models.MembershipStates.JOINED)
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"room_id": self.room.room_id})
        mock_join.assert_not_called()

    def test_invited_member_accepts_invite(self, mock_join):
        member = self._member(self.fixture.admin, models.MembershipStates.INVITED)
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"room_id": self.room.room_id})
        mock_join.assert_called_once_with(self.room.room_id, member.matrix_user_id)
        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.JOINED)

    def test_invite_left_pending_when_join_fails(self, mock_join):
        mock_join.side_effect = MatrixClientError("homeserver down")
        member = self._member(self.fixture.admin, models.MembershipStates.INVITED)
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.INVITED)

    def test_invite_left_pending_when_homeserver_unreachable(self, mock_join):
        mock_join.side_effect = httpx.ConnectError("Connection refused")
        member = self._member(self.fixture.admin, models.MembershipStates.INVITED)
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.INVITED)

    def test_owner_who_is_not_a_member_is_forbidden(self, mock_join):
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mock_join.assert_not_called()

    def test_staff_who_is_not_a_member_is_forbidden(self, mock_join):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_who_left_is_forbidden(self, mock_join):
        self._member(self.fixture.admin, models.MembershipStates.LEFT)
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_room_that_is_not_active_conflicts(self, mock_join):
        self._member(self.fixture.admin, models.MembershipStates.INVITED)
        self.client.force_authenticate(self.fixture.admin)
        for state in (
            models.RoomStates.CREATING,
            models.RoomStates.ARCHIVED,
            models.RoomStates.ERROR,
        ):
            self.room.state = state
            self.room.save(update_fields=["state"])

            response = self.client.post(self.url)

            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, state)
        mock_join.assert_not_called()

    def test_room_without_matrix_id_conflicts(self, mock_join):
        self._member(self.fixture.admin, models.MembershipStates.JOINED)
        self.room.room_id = None
        self.room.save(update_fields=["room_id"])
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_member_who_left_meanwhile_is_not_marked_joined(self, mock_join):
        member = self._member(self.fixture.admin, models.MembershipStates.INVITED)

        def left_while_joining(*args):
            models.MatrixRoomMember.objects.filter(pk=member.pk).update(
                membership_state=models.MembershipStates.LEFT
            )

        mock_join.side_effect = left_while_joining
        self.client.force_authenticate(self.fixture.admin)

        self.client.post(self.url)

        member.refresh_from_db()
        self.assertEqual(member.membership_state, models.MembershipStates.LEFT)

    def test_user_without_access_to_the_room_gets_not_found(self, mock_join):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
