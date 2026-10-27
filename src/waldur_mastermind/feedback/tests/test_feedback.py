from django.utils import timezone
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.feedback import models, utils
from waldur_mastermind.feedback.tests import factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

States = models.FeedbackRequest.States


class FeedbackTestMixin:
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.user = self.fixture.admin
        self.survey = factories.FeedbackSurveyFactory()
        self.question = checklist_factories.QuestionFactory(
            checklist=self.survey.checklist,
            question_type=checklist_enums.QuestionTypes.RATING,
            required=True,
            min_value=1,
            max_value=10,
        )

    def prompt(self, user=None):
        self.client.force_authenticate(user or self.user)
        return self.client.post(factories.FeedbackSurveyFactory.get_list_url("prompt"))

    def unsnooze(self, user=None):
        models.FeedbackRequest.objects.filter(user=user or self.user).update(
            snoozed_until=None
        )


class SurveyPermissionsTest(FeedbackTestMixin, test.APITestCase):
    def test_staff_can_create_survey(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.FeedbackSurveyFactory.get_list_url(),
            {
                "name": "Yearly survey",
                "intro": "Your answers are identified.",
                "checklist": self.survey.checklist.uuid.hex,
                "starts_at": "2026-01-01T00:00:00Z",
                "ends_at": "2026-02-01T00:00:00Z",
                "audience_query": {"customers": [self.customer.uuid.hex]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["audience"]["customers"],
            [{"name": self.customer.name, "uuid": self.customer.uuid.hex}],
        )

    def create(self, **overrides):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "name": "Yearly survey",
            "checklist": self.survey.checklist.uuid.hex,
            "starts_at": "2026-01-01T00:00:00Z",
            "ends_at": "2026-02-01T00:00:00Z",
            "audience_query": {"all_users": True},
            **overrides,
        }
        return self.client.post(
            factories.FeedbackSurveyFactory.get_list_url(), payload, format="json"
        )

    def test_audience_is_required_and_must_match_someone(self):
        self.assertEqual(
            self.create(audience_query={"all_users": False}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        payload = {"name": "x", "checklist": self.survey.checklist.uuid.hex}
        payload.update(starts_at="2026-01-01T00:00:00Z", ends_at="2026-02-01T00:00:00Z")
        response = self.client.post(
            factories.FeedbackSurveyFactory.get_list_url(), payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_prompt_limits_must_be_positive(self):
        for field in ("max_prompts", "snooze_days"):
            self.assertEqual(
                self.create(**{field: 0}).status_code, status.HTTP_400_BAD_REQUEST
            )

    def test_checklist_cannot_change_once_users_were_prompted(self):
        other = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.FEEDBACK
        )
        url = factories.FeedbackSurveyFactory.get_url(self.survey)
        self.client.force_authenticate(self.fixture.staff)
        self.assertEqual(
            self.client.patch(url, {"checklist": other.uuid.hex}).status_code,
            status.HTTP_200_OK,
        )
        self.survey.refresh_from_db()
        self.prompt()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            url, {"checklist": self.question.checklist.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_checklist_must_be_feedback_type(self):
        self.client.force_authenticate(self.fixture.staff)
        checklist = checklist_factories.ChecklistFactory()
        response = self.client.patch(
            factories.FeedbackSurveyFactory.get_url(self.survey),
            {"checklist": checklist.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_support_can_read_but_not_edit(self):
        self.client.force_authenticate(self.fixture.global_support)
        url = factories.FeedbackSurveyFactory.get_url(self.survey)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.patch(url, {"name": "x"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_other_users_cannot_see_surveys(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.FeedbackSurveyFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PromptTest(FeedbackTestMixin, test.APITestCase):
    def test_customer_user_in_audience_is_prompted(self):
        response = self.prompt()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["survey"]["uuid"], self.survey.uuid.hex)
        request = models.FeedbackRequest.objects.get(user=self.user)
        self.assertEqual(request.prompt_count, 1)
        self.assertEqual(request.organization_names, [self.customer.name])
        self.assertIsNotNone(request.checklist_completion)

    def test_staff_and_support_are_not_prompted(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            self.assertEqual(self.prompt(user).status_code, status.HTTP_204_NO_CONTENT)

    def test_user_outside_audience_is_not_prompted(self):
        self.survey.audience = {
            "customers": [{"uuid": structure_factories.CustomerFactory().uuid.hex}]
        }
        self.survey.save()
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)

    def test_offering_consumers_are_prompted(self):
        resource = marketplace_factories.ResourceFactory(project=self.fixture.project)
        self.survey.audience = {"offerings": [{"uuid": resource.offering.uuid.hex}]}
        self.survey.save()
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        other = structure_fixtures.ProjectFixture()
        self.assertEqual(
            self.prompt(other.admin).status_code, status.HTTP_204_NO_CONTENT
        )

    def test_organizations_and_offerings_together_mean_both(self):
        offering = marketplace_factories.OfferingFactory()
        self.survey.audience = {
            "customers": [{"uuid": self.customer.uuid.hex}],
            "offerings": [{"uuid": offering.uuid.hex}],
        }
        self.survey.save()
        # A member of the organisation without a resource on the offering.
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)
        marketplace_factories.ResourceFactory(
            project=self.fixture.project, offering=offering
        )
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        # A consumer of the offering outside the listed organisations.
        other = structure_fixtures.ProjectFixture()
        marketplace_factories.ResourceFactory(project=other.project, offering=offering)
        self.assertEqual(
            self.prompt(other.admin).status_code, status.HTTP_204_NO_CONTENT
        )

    def test_survey_outside_window_is_not_shown(self):
        self.survey.starts_at = timezone.now() + timezone.timedelta(days=1)
        self.survey.ends_at = timezone.now() + timezone.timedelta(days=2)
        self.survey.save()
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)

    def test_inactive_survey_is_not_shown(self):
        self.survey.is_active = False
        self.survey.save()
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)

    def test_prompt_stops_after_max_prompts(self):
        self.survey.max_prompts = 2
        self.survey.save()
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        self.unsnooze()
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        self.unsnooze()
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)

    def test_shown_prompt_is_snoozed_so_other_tabs_do_not_count(self):
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)
        request = models.FeedbackRequest.objects.get(user=self.user)
        self.assertEqual(request.prompt_count, 1)
        self.assertIsNotNone(request.snoozed_until)

    def test_get_does_not_count_a_prompt(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(
            factories.FeedbackSurveyFactory.get_list_url("prompt")
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertFalse(models.FeedbackRequest.objects.exists())

    def test_malformed_audience_does_not_break_prompt(self):
        self.survey.audience = {"customers": [self.customer.uuid.hex, "junk"]}
        self.survey.save()
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)

    def test_survey_ending_soonest_wins(self):
        sooner = factories.FeedbackSurveyFactory(
            ends_at=timezone.now() + timezone.timedelta(days=1)
        )
        self.assertEqual(self.prompt().data["survey"]["uuid"], sooner.uuid.hex)


class RequestActionsTest(FeedbackTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.prompt()
        self.request = models.FeedbackRequest.objects.get(user=self.user)

    def submit(self, value, user=None):
        self.client.force_authenticate(user or self.user)
        return self.client.post(
            factories.get_request_url(self.request, "submit_answers"),
            [{"question_uuid": self.question.uuid.hex, "answer_data": value}],
            format="json",
        )

    def test_complete_submission_answers_request_and_logs_event(self):
        response = self.submit(8)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.ANSWERED)
        self.assertIsNotNone(self.request.answered_at)
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type=EventType.FEEDBACK_SURVEY_ANSWERED
            ).exists()
        )
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)

    def test_out_of_range_rating_is_rejected(self):
        self.assertEqual(self.submit(11).status_code, status.HTTP_400_BAD_REQUEST)

    def test_incomplete_submission_keeps_request_pending(self):
        optional = checklist_factories.QuestionFactory(
            checklist=self.survey.checklist,
            question_type=checklist_enums.QuestionTypes.TEXT_AREA,
        )
        self.client.force_authenticate(self.user)
        self.client.post(
            factories.get_request_url(self.request, "submit_answers"),
            [{"question_uuid": optional.uuid.hex, "answer_data": "Fine"}],
            format="json",
        )
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.PENDING)

    def test_hidden_required_question_does_not_block_answered(self):
        hidden = checklist_factories.QuestionFactory(
            checklist=self.survey.checklist,
            question_type=checklist_enums.QuestionTypes.TEXT_AREA,
            required=True,
        )
        checklist_models.QuestionDependency.objects.create(
            question=hidden,
            depends_on_question=self.question,
            required_answer_value=1,
            operator="equals",
        )
        self.submit(8)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.ANSWERED)

    def test_owner_reads_checklist(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(factories.get_request_url(self.request, "checklist"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["questions"][0]["uuid"], self.question.uuid.hex)

    def test_empty_submission_does_not_answer_optional_survey(self):
        self.question.required = False
        self.question.save()
        self.client.force_authenticate(self.user)
        self.client.post(
            factories.get_request_url(self.request, "submit_answers"), [], format="json"
        )
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.PENDING)

    def test_closed_survey_cannot_be_answered(self):
        self.survey.ends_at = timezone.now() - timezone.timedelta(minutes=1)
        self.survey.save()
        self.assertEqual(self.submit(8).status_code, status.HTTP_409_CONFLICT)
        self.client.force_authenticate(self.user)
        for action in ("later", "dismiss"):
            self.assertEqual(
                self.client.post(
                    factories.get_request_url(self.request, action)
                ).status_code,
                status.HTTP_409_CONFLICT,
            )

    def test_answered_request_cannot_be_dismissed(self):
        self.submit(8)
        self.client.force_authenticate(self.user)
        response = self.client.post(factories.get_request_url(self.request, "dismiss"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.ANSWERED)

    def test_answers_carry_the_rating_scale(self):
        self.submit(8)
        self.client.force_authenticate(self.fixture.global_support)
        answer = self.client.get(factories.get_request_url()).data[0]["answers"][0]
        self.assertEqual(float(answer["max_value"]), 10)

    def test_expired_when_survey_ends_or_prompts_run_out(self):
        self.request.snoozed_until = timezone.now() - timezone.timedelta(minutes=1)
        self.request.save()
        utils.expire_requests()
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.PENDING)
        self.survey.max_prompts = 1
        self.survey.save()
        utils.expire_requests()
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.EXPIRED)

    def test_later_keeps_answers_as_a_draft(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            factories.get_request_url(self.request, "later"),
            [{"question_uuid": self.question.uuid.hex, "answer_data": 7}],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.PENDING)
        self.assertIsNotNone(self.request.snoozed_until)
        self.assertEqual(self.request.checklist_completion.answers.get().answer_data, 7)

    def test_later_keeps_valid_answers_when_one_is_invalid(self):
        comment = checklist_factories.QuestionFactory(
            checklist=self.survey.checklist,
            question_type=checklist_enums.QuestionTypes.TEXT_AREA,
        )
        self.client.force_authenticate(self.user)
        response = self.client.post(
            factories.get_request_url(self.request, "later"),
            [
                {"question_uuid": comment.uuid.hex, "answer_data": "Draft text"},
                {"question_uuid": self.question.uuid.hex, "answer_data": 11},
            ],
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.PENDING)
        answers = self.request.checklist_completion.answers
        self.assertEqual(answers.get().question, comment)
        self.assertEqual(answers.get().answer_data, "Draft text")

    def test_later_snoozes_prompt(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(factories.get_request_url(self.request, "later"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)
        self.request.refresh_from_db()
        self.request.snoozed_until = timezone.now() - timezone.timedelta(minutes=1)
        self.request.save()
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)

    def test_dismiss_stops_prompt_for_good(self):
        self.client.force_authenticate(self.user)
        self.client.post(factories.get_request_url(self.request, "dismiss"))
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, States.DISMISSED)
        self.assertEqual(self.prompt().status_code, status.HTTP_204_NO_CONTENT)

    def test_only_owner_can_act_on_request(self):
        for user in (self.fixture.owner, self.fixture.staff):
            self.assertIn(
                self.submit(8, user).status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
            self.client.force_authenticate(user)
            self.assertIn(
                self.client.post(
                    factories.get_request_url(self.request, "dismiss")
                ).status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )

    def test_users_see_only_own_requests(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(self.client.get(factories.get_request_url()).data, [])

    def test_support_lists_requests_with_answers(self):
        self.submit(8)
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(
            factories.get_request_url(), {"customer_uuid": self.customer.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["answers"][0]["answer"], 8)
        response = self.client.get(
            factories.get_request_url(),
            {"customer_uuid": structure_factories.CustomerFactory().uuid.hex},
        )
        self.assertEqual(response.data, [])


class SummaryTest(FeedbackTestMixin, test.APITestCase):
    def answer(self, question, value, state=States.ANSWERED):
        fixture = structure_fixtures.ProjectFixture()
        self.prompt(fixture.admin)
        request = models.FeedbackRequest.objects.get(
            survey=self.survey, user=fixture.admin
        )
        request.state = state
        request.save()
        checklist_factories.AnswerFactory(
            completion=request.checklist_completion,
            question=question,
            user=fixture.admin,
            answer_data=value,
        )

    def test_summary_counts_ratings_likert_and_options(self):
        likert = checklist_factories.QuestionFactory(
            checklist=self.survey.checklist,
            question_type=checklist_enums.QuestionTypes.LIKERT,
            likert_scale_length=5,
        )
        select = checklist_factories.QuestionFactory(
            checklist=self.survey.checklist,
            question_type=checklist_enums.QuestionTypes.SINGLE_SELECT,
        )
        option = checklist_factories.QuestionOptionFactory(question=select)
        self.answer(self.question, 4)
        self.answer(self.question, "8")
        self.answer(likert, 0)
        self.answer(select, [str(option.uuid)])
        self.answer(self.question, 1, state=States.PENDING)

        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(
            factories.FeedbackSurveyFactory.get_url(self.survey, "summary")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["prompted"], 5)
        self.assertEqual(response.data["answered"], 4)
        self.assertEqual(response.data["pending"], 1)
        rating, likert_result, select_result = response.data["questions"]
        self.assertEqual(rating["mean"], 6)
        self.assertEqual(
            [(item["value"], item["count"]) for item in rating["distribution"]],
            [("4", 1), ("8", 1)],
        )
        self.assertEqual(likert_result["distribution"][0]["count"], 1)
        self.assertEqual(likert_result["distribution"][0]["value"], "1")
        self.assertEqual(select_result["distribution"][0]["count"], 1)
