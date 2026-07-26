from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class ChatThreadStatsTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("chat-thread-stats")

    def _message(self, thread, index, **kwargs):
        return Message.objects.create(
            thread=thread,
            role=Message.Role.USER,
            blocks=[],
            sequence_index=index,
            **kwargs,
        )

    def test_stats_aggregates_visible_threads(self):
        user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=user)
        thread = ThreadSession.objects.create(chat_session=session)
        self._message(thread, 1, input_tokens=10, output_tokens=20, feedback_score=True)
        self._message(
            thread,
            2,
            input_tokens=5,
            output_tokens=5,
            feedback_score=False,
            is_flagged=True,
        )

        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["threads_total"], 1)
        self.assertEqual(data["sessions_total"], 1)
        self.assertEqual(data["users_total"], 1)
        self.assertEqual(data["messages_total"], 2)
        self.assertEqual(data["input_tokens_total"], 15)
        self.assertEqual(data["output_tokens_total"], 25)
        self.assertEqual(data["total_tokens"], 40)
        self.assertEqual(data["flagged_total"], 1)
        self.assertEqual(data["feedback_positive"], 1)
        self.assertEqual(data["feedback_negative"], 1)
        self.assertEqual(data["satisfaction_rate"], 0.5)

    def test_satisfaction_rate_pins_thumbs_direction(self):
        # Asymmetric on purpose: 2 up + 1 down. A symmetric fixture would still
        # read 0.5 if the up/down mapping were inverted — this one wouldn't.
        user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=user)
        thread = ThreadSession.objects.create(chat_session=session)
        self._message(thread, 1, feedback_score=True)
        self._message(thread, 2, feedback_score=True)
        self._message(thread, 3, feedback_score=False)

        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["feedback_positive"], 2)
        self.assertEqual(data["feedback_negative"], 1)
        self.assertEqual(round(data["satisfaction_rate"], 3), 0.667)

    def test_flagged_total_counts_threads_not_flagged_messages(self):
        # Two flagged messages on one thread. flagged_total joins through
        # messages, so without distinct=True this would inflate to 2 — the
        # count is threads-with-a-flag, not flagged messages.
        user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=user)
        thread = ThreadSession.objects.create(chat_session=session)
        self._message(thread, 1, is_flagged=True)
        self._message(thread, 2, is_flagged=True)

        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["flagged_total"], 1)

    def test_satisfaction_rate_is_null_without_feedback(self):
        user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=user)
        thread = ThreadSession.objects.create(chat_session=session)
        self._message(thread, 1)

        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["satisfaction_rate"])

    def test_regular_user_excludes_other_users_threads(self):
        user = structure_factories.UserFactory()
        other = structure_factories.UserFactory()
        other_session = ChatSession.objects.create(user=other)
        ThreadSession.objects.create(chat_session=other_session)

        self.client.force_authenticate(user=user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["threads_total"], 0)

    def test_staff_sees_all_threads(self):
        owner = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=owner)
        ThreadSession.objects.create(chat_session=session)
        staff = structure_factories.UserFactory(is_staff=True)

        self.client.force_authenticate(user=staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["threads_total"], 1)
