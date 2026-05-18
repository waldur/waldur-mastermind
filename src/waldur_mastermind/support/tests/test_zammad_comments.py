from unittest import mock

import pytest
from rest_framework import test

from waldur_mastermind.support.backend import SupportBackendType
from waldur_mastermind.support.backend.zammad_utils import ZammadBackend

WALDUR_ZAMMAD_USER_ID = "5"


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE=SupportBackendType.ZAMMAD,
    ZAMMAD_API_URL="http://localhost:8080",
    ZAMMAD_TOKEN="token",
    ZAMMAD_COMMENT_MARKER="Created by Waldur",
    ZAMMAD_COMMENT_PREFIX="User: {name}",
)
class ZammadCommentMarkerTest(test.APITestCase):
    """
    Regression tests for ONS: staff replies to user comments on Zammad
    were dropped during sync because the body contained the Waldur marker
    string carried over from a quoted previous Waldur comment.

    Waldur-originated comments are now detected by the author identity
    (the Zammad user Waldur authenticates as), not by a body-text marker.
    """

    def setUp(self):
        self.backend = ZammadBackend()
        # The Zammad user Waldur authenticates as (avoids a real API call).
        me_patch = mock.patch(
            "zammad_py.api.User.me",
            return_value={"id": WALDUR_ZAMMAD_USER_ID},
        )
        me_patch.start()
        self.addCleanup(me_patch.stop)

    def _make_response(self, body, created_by_id):
        return {
            "id": 42,
            "ticket_id": 7,
            "from": "Agent Smith",
            "created_at": "2026-05-18T10:00:00Z",
            "created_by_id": created_by_id,
            "internal": False,
            "body": body,
            "attachments": [],
            "sender": "Agent",
        }

    def test_genuine_waldur_comment_is_detected(self):
        """A comment Waldur itself posted must be detected and skipped."""
        body = "Here is my answer.\n\nCreated by Waldur\n\nUser: Alice"

        comment = self.backend._zammad_response_to_comment(
            self._make_response(body, created_by_id=WALDUR_ZAMMAD_USER_ID)
        )

        self.assertTrue(
            comment.is_waldur_comment,
            "Genuine Waldur-originated comment must be detected as such.",
        )

    def test_native_staff_comment_without_marker_is_not_waldur(self):
        """A plain staff comment with no marker must sync to Waldur."""
        body = "Thanks for reaching out, we are looking into it."

        comment = self.backend._zammad_response_to_comment(
            self._make_response(body, created_by_id="99")
        )

        self.assertFalse(comment.is_waldur_comment)

    def test_staff_reply_quoting_waldur_comment_is_not_waldur(self):
        """
        The staff reply is authored by the agent (not the Waldur API user)
        and MUST reach Waldur, even though its body contains the marker.
        """
        body = (
            "Hi, your VM has been resized as requested.\n\n"
            "On 2026-05-17 the customer wrote:\n\n"
            "Created by Waldur\n\n"
            "User: Alice\n\n"
            "Please resize my VM."
        )

        comment = self.backend._zammad_response_to_comment(
            self._make_response(body, created_by_id="99")
        )

        self.assertFalse(
            comment.is_waldur_comment,
            "A staff reply that merely quotes a previous Waldur comment "
            "must not be treated as a Waldur comment.",
        )
