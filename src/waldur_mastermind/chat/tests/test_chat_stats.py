import datetime

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

    def test_stats_honours_annotation_backed_filters(self):
        # These filters resolve against annotations that only exist on
        # get_queryset(); aggregating over a bare queryset made every one of
        # them a 500, and the summary widget sends them verbatim.
        user = structure_factories.UserFactory(is_staff=True)
        session = ChatSession.objects.create(user=user)
        rated = ThreadSession.objects.create(chat_session=session)
        self._message(rated, 1, input_tokens=10, output_tokens=20, feedback_score=True)
        unrated = ThreadSession.objects.create(chat_session=session)
        self._message(unrated, 1, input_tokens=500, output_tokens=500)

        self.client.force_authenticate(user=user)
        response = self.client.get(self.url, {"has_feedback": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["threads_total"], 1)
        self.assertEqual(response.data["input_tokens_total"], 10)

    def test_stats_honours_token_range_filters(self):
        user = structure_factories.UserFactory(is_staff=True)
        session = ChatSession.objects.create(user=user)
        cheap = ThreadSession.objects.create(chat_session=session)
        self._message(cheap, 1, input_tokens=10, output_tokens=20)
        pricey = ThreadSession.objects.create(chat_session=session)
        self._message(pricey, 1, input_tokens=500, output_tokens=500)

        self.client.force_authenticate(user=user)
        response = self.client.get(self.url, {"total_tokens_min": 100})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["threads_total"], 1)
        self.assertEqual(response.data["total_tokens"], 1000)

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


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class ChatThreadCreatedRangeFilterTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("chat-thread-stats")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.session = ChatSession.objects.create(user=self.staff)
        self.client.force_authenticate(user=self.staff)

    def _thread_on(self, year, month, day):
        thread = ThreadSession.objects.create(chat_session=self.session)
        # Late in the day on purpose: a DateTimeFilter with a plain `lte` lookup
        # would resolve created_before=<day> to midnight and drop this row.
        # That is the bug this filter must not have.
        ThreadSession.objects.filter(pk=thread.pk).update(
            created=datetime.datetime(year, month, day, 23, 30, tzinfo=datetime.UTC)
        )
        return thread

    def test_range_is_inclusive_of_both_boundary_days(self):
        self._thread_on(2026, 6, 30)
        self._thread_on(2026, 7, 1)
        self._thread_on(2026, 7, 31)
        self._thread_on(2026, 8, 1)

        response = self.client.get(
            self.url, {"created_after": "2026-07-01", "created_before": "2026-07-31"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["threads_total"], 2)

    def test_range_narrows_token_aggregates(self):
        june = self._thread_on(2026, 6, 15)
        july = self._thread_on(2026, 7, 15)
        for index, thread in enumerate((june, july)):
            Message.objects.create(
                thread=thread,
                role=Message.Role.ASSISTANT,
                blocks=[],
                sequence_index=index,
                input_tokens=100,
                output_tokens=10,
            )

        response = self.client.get(
            self.url, {"created_after": "2026-07-01", "created_before": "2026-07-31"}
        )

        self.assertEqual(response.data["input_tokens_total"], 100)
        self.assertEqual(response.data["output_tokens_total"], 10)

    def test_old_exact_day_param_is_gone(self):
        self._thread_on(2026, 7, 15)
        # `created` was an exact-day equality filter. Reusing the name as a
        # range bound would silently change what existing URLs mean, so it is
        # removed outright: django-filter ignores unknown params.
        response = self.client.get(self.url, {"created": "2026-01-01"})

        self.assertEqual(response.data["threads_total"], 1)


class MessageModelFieldTest(test.APITestCase):
    def _thread(self):
        user = structure_factories.UserFactory()
        session = ChatSession.objects.create(user=user)
        return ThreadSession.objects.create(chat_session=session)

    def test_model_defaults_to_empty_meaning_pre_tracking(self):
        message = Message.objects.create(
            thread=self._thread(),
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
        )

        self.assertEqual(message.model, "")

    def test_model_round_trips(self):
        Message.objects.create(
            thread=self._thread(),
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
            model="qwen3.5-122b-nothinking",
        )

        self.assertEqual(
            Message.objects.get(sequence_index=1).model, "qwen3.5-122b-nothinking"
        )


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class ThreadModelsUsedTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("chat-thread-list")
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.session = ChatSession.objects.create(user=self.staff)
        self.thread = ThreadSession.objects.create(chat_session=self.session)
        self.client.force_authenticate(user=self.staff)

    def _message(self, index, model):
        return Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=index,
            model=model,
        )

    def test_reports_the_single_model_a_thread_used(self):
        self._message(1, "new-model")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["models_used"], "new-model")

    def test_lists_every_model_when_a_thread_spans_a_switch(self):
        # An admin can flip AI_ASSISTANT_MODEL mid-thread. Naming one of them
        # would be a lie and "mixed" would hide which; both are listed.
        self._message(1, "old-model")
        self._message(2, "new-model")

        response = self.client.get(self.url)

        self.assertEqual(
            sorted(response.data[0]["models_used"].split(", ")),
            ["new-model", "old-model"],
        )

    def test_is_blank_for_pre_tracking_threads(self):
        self._message(1, "")

        response = self.client.get(self.url)

        self.assertEqual(response.data[0]["models_used"], "")

    def test_does_not_multiply_the_message_count(self):
        # Aggregating models over the same messages join as message_count is
        # where a stray row-multiplication would show up.
        self._message(1, "old-model")
        self._message(2, "new-model")

        response = self.client.get(self.url)

        self.assertEqual(response.data[0]["message_count"], 2)

    def test_threads_can_be_ordered_by_model(self):
        # The frontend renders a sort control on the Model column; without this
        # ordering field it would be a control that silently does nothing.
        self._message(1, "zeta-model")
        other = ThreadSession.objects.create(chat_session=self.session)
        Message.objects.create(
            thread=other,
            role=Message.Role.ASSISTANT,
            blocks=[],
            sequence_index=1,
            model="alpha-model",
        )

        response = self.client.get(self.url, {"o": "models_used"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["models_used"] for row in response.data],
            ["alpha-model", "zeta-model"],
        )
