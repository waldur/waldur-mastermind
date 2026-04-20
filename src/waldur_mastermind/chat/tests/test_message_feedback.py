from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession

# Shared Constance override: the chat API requires AI_ASSISTANT_ENABLED at
# dispatch time. Applied to each class below.
_FEEDBACK_CONSTANCE = override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)


@_FEEDBACK_CONSTANCE
class MessageFeedbackFieldsTest(test.APITestCase):
    """Test that feedback fields appear in message list responses."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)
        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)
        self.list_url = reverse("chat-message-list")

    def test_feedback_fields_present_in_response(self):
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data[0]
        self.assertIn("feedback_score", data)
        self.assertIn("feedback_comment", data)
        self.assertIn("feedback_category", data)
        self.assertIn("feedback_submitted_at", data)
        self.assertIsNone(data["feedback_score"])
        self.assertIsNone(data["feedback_comment"])
        self.assertIsNone(data["feedback_category"])
        self.assertIsNone(data["feedback_submitted_at"])


@_FEEDBACK_CONSTANCE
class MessageFeedbackActionTest(test.APITestCase):
    """Test POST /api/chat-messages/{uuid}/feedback/ endpoint."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)
        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)
        self.assistant_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=2,
        )
        self.feedback_url = reverse(
            "chat-message-feedback", kwargs={"uuid": self.assistant_msg.uuid}
        )

    def test_thumbs_up_succeeds(self):
        response = self.client.post(self.feedback_url, {"score": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertTrue(self.assistant_msg.feedback_score)
        self.assertIsNone(self.assistant_msg.feedback_comment)

    def test_thumbs_down_with_comment_succeeds(self):
        response = self.client.post(
            self.feedback_url,
            {"score": False, "comment": "Hallucinated a resource"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertFalse(self.assistant_msg.feedback_score)
        self.assertEqual(self.assistant_msg.feedback_comment, "Hallucinated a resource")

    def test_thumbs_down_without_comment_succeeds(self):
        response = self.client.post(
            self.feedback_url,
            {"score": False},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertFalse(self.assistant_msg.feedback_score)
        self.assertIsNone(self.assistant_msg.feedback_comment)

    def test_thumbs_down_with_category_succeeds(self):
        response = self.client.post(
            self.feedback_url,
            {"score": False, "category": "inaccurate"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertFalse(self.assistant_msg.feedback_score)
        self.assertEqual(self.assistant_msg.feedback_category, "inaccurate")

    def test_thumbs_down_with_category_and_comment_succeeds(self):
        response = self.client.post(
            self.feedback_url,
            {
                "score": False,
                "category": "other",
                "comment": "Something else entirely",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertEqual(self.assistant_msg.feedback_category, "other")
        self.assertEqual(self.assistant_msg.feedback_comment, "Something else entirely")

    def test_category_with_thumbs_up_is_rejected(self):
        response = self.client.post(
            self.feedback_url,
            {"score": True, "category": "inaccurate"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_category_is_rejected(self):
        response = self.client.post(
            self.feedback_url,
            {"score": False, "category": "made_up_category"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_comment_with_thumbs_up_succeeds(self):
        response = self.client.post(
            self.feedback_url,
            {"score": True, "comment": "Great answer!"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertTrue(self.assistant_msg.feedback_score)
        self.assertEqual(self.assistant_msg.feedback_comment, "Great answer!")

    def test_feedback_on_user_message_is_rejected(self):
        user_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=[],
            sequence_index=1,
        )
        url = reverse("chat-message-feedback", kwargs={"uuid": user_msg.uuid})

        response = self.client.post(url, {"score": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_overwriting_thumbs_up_with_thumbs_down_succeeds(self):
        self.client.post(self.feedback_url, {"score": True}, format="json")

        response = self.client.post(
            self.feedback_url,
            {"score": False, "comment": "actually wrong", "category": "inaccurate"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertFalse(self.assistant_msg.feedback_score)
        self.assertEqual(self.assistant_msg.feedback_comment, "actually wrong")
        self.assertEqual(self.assistant_msg.feedback_category, "inaccurate")

    def test_flipping_to_thumbs_up_wipes_comment_and_category(self):
        self.assistant_msg.feedback_score = False
        self.assistant_msg.feedback_comment = "old comment"
        self.assistant_msg.feedback_category = "inaccurate"
        self.assistant_msg.save(
            update_fields=[
                "feedback_score",
                "feedback_comment",
                "feedback_category",
            ]
        )

        response = self.client.post(self.feedback_url, {"score": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertTrue(self.assistant_msg.feedback_score)
        self.assertIsNone(self.assistant_msg.feedback_comment)
        self.assertIsNone(self.assistant_msg.feedback_category)

    def test_resubmitting_same_score_updates_detail(self):
        self.client.post(
            self.feedback_url,
            {"score": False, "comment": "first"},
            format="json",
        )

        response = self.client.post(
            self.feedback_url,
            {"score": False, "comment": "second", "category": "other"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertEqual(self.assistant_msg.feedback_comment, "second")
        self.assertEqual(self.assistant_msg.feedback_category, "other")

    def test_feedback_on_other_users_message_returns_404(self):
        other_user = structure_factories.UserFactory()
        other_session = ChatSession.objects.create(user=other_user)
        other_thread = ThreadSession.objects.create(chat_session=other_session)
        other_msg = Message.objects.create(
            thread=other_thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
        )
        url = reverse("chat-message-feedback", kwargs={"uuid": other_msg.uuid})

        response = self.client.post(url, {"score": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_feedback_without_score_is_rejected(self):
        response = self.client.post(self.feedback_url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_comment_over_max_length_is_rejected(self):
        response = self.client.post(
            self.feedback_url,
            {"score": False, "comment": "x" * 2001},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_boolean_score_value_is_rejected(self):
        # DRF BooleanField rejects unrecognized strings like "maybe".
        response = self.client.post(
            self.feedback_url,
            {"score": "maybe"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_feedback_submitted_at_is_stamped(self):
        with freeze_time("2026-04-18 12:00:00"):
            response = self.client.post(
                self.feedback_url, {"score": True}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assistant_msg.refresh_from_db()
        self.assertIsNotNone(self.assistant_msg.feedback_submitted_at)
        self.assertEqual(
            self.assistant_msg.feedback_submitted_at.isoformat(),
            "2026-04-18T12:00:00+00:00",
        )

    def test_feedback_submitted_at_updates_on_resubmit(self):
        with freeze_time("2026-04-18 12:00:00"):
            self.client.post(self.feedback_url, {"score": True}, format="json")
        self.assistant_msg.refresh_from_db()
        first_ts = self.assistant_msg.feedback_submitted_at

        with freeze_time("2026-04-18 12:05:00"):
            self.client.post(self.feedback_url, {"score": False}, format="json")
        self.assistant_msg.refresh_from_db()

        self.assertGreater(self.assistant_msg.feedback_submitted_at, first_ts)

    def test_feedback_submission_emits_audit_event(self):
        response = self.client.post(
            self.feedback_url,
            {"score": False, "category": "inaccurate", "comment": "wrong"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        events = Event.objects.filter(event_type="chat_feedback_submitted")
        self.assertEqual(events.count(), 1)
        ctx = events.first().context
        self.assertEqual(ctx["score"], False)
        self.assertEqual(ctx["category"], "inaccurate")
        self.assertEqual(ctx["message_uuid"], self.assistant_msg.uuid.hex)


@_FEEDBACK_CONSTANCE
class MessageFeedbackFilterTest(test.APITestCase):
    """Test filtering messages by feedback_score."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)

        self.user = structure_factories.UserFactory()
        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)
        self.list_url = reverse("chat-message-list")

        self.msg_positive = Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
            feedback_score=True,
        )
        self.msg_negative = Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=2,
            feedback_score=False,
            feedback_comment="Wrong info",
        )
        self.msg_no_feedback = Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=3,
        )

    def test_filter_thumbs_down(self):
        response = self.client.get(self.list_url, {"feedback_score": "false"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.msg_negative.uuid))

    def test_filter_thumbs_up(self):
        response = self.client.get(self.list_url, {"feedback_score": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.msg_positive.uuid))


@_FEEDBACK_CONSTANCE
class ThreadFeedbackFilterTest(test.APITestCase):
    """Test filtering threads by has_feedback."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)

        self.user = structure_factories.UserFactory()
        self.session = ChatSession.objects.create(user=self.user)
        self.list_url = reverse("chat-thread-list")

        self.thread_with_feedback = ThreadSession.objects.create(
            chat_session=self.session
        )
        Message.objects.create(
            thread=self.thread_with_feedback,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
            feedback_score=False,
        )

        self.thread_without_feedback = ThreadSession.objects.create(
            chat_session=self.session
        )
        Message.objects.create(
            thread=self.thread_without_feedback,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
        )

    def test_has_feedback_field_in_response(self):
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids_to_data = {d["uuid"]: d for d in response.data}
        self.assertIn(
            "has_feedback", uuids_to_data[str(self.thread_with_feedback.uuid)]
        )
        self.assertTrue(
            uuids_to_data[str(self.thread_with_feedback.uuid)]["has_feedback"]
        )
        self.assertFalse(
            uuids_to_data[str(self.thread_without_feedback.uuid)]["has_feedback"]
        )

    def test_filter_threads_with_feedback(self):
        response = self.client.get(self.list_url, {"has_feedback": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {d["uuid"] for d in response.data}
        self.assertIn(str(self.thread_with_feedback.uuid), uuids)
        self.assertNotIn(str(self.thread_without_feedback.uuid), uuids)

    def test_filter_threads_without_feedback(self):
        response = self.client.get(self.list_url, {"has_feedback": "false"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {d["uuid"] for d in response.data}
        self.assertIn(str(self.thread_without_feedback.uuid), uuids)
        self.assertNotIn(str(self.thread_with_feedback.uuid), uuids)


@_FEEDBACK_CONSTANCE
class MessageFeedbackAuthorizationTest(test.APITestCase):
    """Staff/support can LIST any user's messages but cannot submit feedback
    on someone else's thread — the feedback action scopes by request.user."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.staff)

        self.other_user = structure_factories.UserFactory()
        self.other_session = ChatSession.objects.create(user=self.other_user)
        self.other_thread = ThreadSession.objects.create(
            chat_session=self.other_session
        )
        self.other_msg = Message.objects.create(
            thread=self.other_thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
        )

    def test_staff_cannot_submit_feedback_on_other_users_message(self):
        url = reverse("chat-message-feedback", kwargs={"uuid": self.other_msg.uuid})

        response = self.client.post(url, {"score": True}, format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.other_msg.refresh_from_db()
        self.assertIsNone(self.other_msg.feedback_score)
