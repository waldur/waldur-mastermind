import uuid
from unittest import mock

from django.utils import timezone
from rest_framework import serializers, status, test

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
from waldur_mastermind.proposal.tests import factories as proposal_factories

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

    def create(self, user=None, **overrides):
        self.client.force_authenticate(user or self.fixture.staff)
        payload = {
            "name": "Yearly survey",
            "starts_at": "2026-01-01T00:00:00Z",
            "ends_at": "2026-02-01T00:00:00Z",
            "audience_query": {"all_users": True},
            **overrides,
        }
        return self.client.post(
            factories.FeedbackSurveyFactory.get_list_url(), payload, format="json"
        )

    def questions(self):
        return [
            {
                "key": "rating",
                "description": "How satisfied are you?",
                "question_type": checklist_enums.QuestionTypes.RATING,
                "min_value": 1,
                "max_value": 5,
                "conditions": [
                    {
                        "depends_on": "used",
                        "operator": "in",
                        "required_answer_value": ["opt-yes"],
                    }
                ],
            },
            {
                "key": "used",
                "description": "Have you used the portal?",
                "question_type": checklist_enums.QuestionTypes.SINGLE_SELECT,
                "options": [
                    {"key": "opt-yes", "label": "Yes"},
                    {"key": "opt-no", "label": "No"},
                ],
            },
        ]

    def counts(self):
        return (
            checklist_models.Checklist.objects.count(),
            checklist_models.Question.objects.count(),
            models.FeedbackSurvey.objects.count(),
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
                "questions": self.questions(),
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

    def create(self, user=None, **overrides):
        overrides.setdefault("questions", self.questions())
        return super().create(user, **overrides)

    def test_audience_is_required_and_must_match_someone(self):
        self.assertEqual(
            self.create(audience_query={"all_users": False}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        payload = {"name": "x", "questions": self.questions()}
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

    def test_support_can_create_and_edit(self):
        support = self.fixture.global_support
        response = super().create(support, questions=self.questions())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        url = factories.FeedbackSurveyFactory.get_url(self.survey)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.patch(url, {"name": "x"}).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT
        )

    def test_customer_owner_cannot(self):
        owner = self.fixture.owner
        response = super().create(owner, questions=self.questions())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        url = factories.FeedbackSurveyFactory.get_url(self.survey)
        self.assertEqual(
            self.client.patch(url, {"name": "x"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_403_FORBIDDEN)
        self.survey.refresh_from_db()
        self.assertNotEqual(self.survey.name, "x")

    def test_other_users_cannot_see_surveys(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.FeedbackSurveyFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SurveyNestedCreateTest(FeedbackTestMixin, test.APITestCase):
    def test_create_with_questions_builds_owned_checklist(self):
        response = self.create(questions=self.questions())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        survey = models.FeedbackSurvey.objects.get(uuid=response.data["uuid"])
        checklist = survey.checklist
        self.assertNotEqual(checklist, self.survey.checklist)
        self.assertEqual(checklist.name, "Yearly survey")
        self.assertEqual(
            checklist.checklist_type, checklist_enums.ChecklistTypes.FEEDBACK
        )

        rating, used = checklist.get_questions()
        self.assertEqual(rating.description, "How satisfied are you?")
        self.assertEqual(used.description, "Have you used the portal?")
        dependency = checklist_models.QuestionDependency.objects.get()
        self.assertEqual(dependency.question, rating)
        self.assertEqual(dependency.depends_on_question, used)
        self.assertEqual(
            dependency.required_answer_value,
            [used.question_options.get(label="Yes").uuid.hex],
        )

    def test_create_needs_questions(self):
        before = self.counts()
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("questions", response.data)
        self.assertEqual(self.counts(), before)

    def test_checklist_cannot_be_given(self):
        before = self.counts()
        response = self.create(checklist=self.survey.checklist.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("questions", response.data)
        self.assertEqual(self.counts(), before)

        response = self.create(
            checklist=self.survey.checklist.uuid.hex, questions=self.questions()
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertNotIn("checklist", response.data)
        self.assertNotIn("checklist_name", response.data)
        survey = models.FeedbackSurvey.objects.get(uuid=response.data["uuid"])
        self.assertNotEqual(survey.checklist, self.survey.checklist)

        response = self.client.patch(
            factories.FeedbackSurveyFactory.get_url(self.survey),
            {"checklist": survey.checklist.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        checklist = self.survey.checklist
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.checklist, checklist)

    def test_invalid_questions_leave_nothing_behind(self):
        payload = self.questions()
        payload[1]["description"] = payload[0]["description"]
        before = self.counts()
        response = self.create(questions=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("questions", response.data)
        self.assertEqual(self.counts(), before)

    def test_question_count_is_capped(self):
        payload = [
            {
                "key": f"q{i}",
                "description": f"Question {i}",
                "question_type": checklist_enums.QuestionTypes.TEXT_INPUT,
            }
            for i in range(101)
        ]
        response = self.create(questions=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("questions", response.data)

    def test_questions_cannot_be_empty(self):
        before = self.counts()
        response = self.create(questions=[])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("questions", response.data)
        self.assertEqual(self.counts(), before)

    def test_failed_create_leaves_nothing_behind(self):
        before = self.counts()
        with mock.patch(
            "waldur_mastermind.feedback.serializers.create_question_set",
            side_effect=serializers.ValidationError("boom"),
        ):
            response = self.create(questions=self.questions())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.counts(), before)


class SurveyQuestionsEditTest(FeedbackTestMixin, test.APITestCase):
    def round_trip_questions(self):
        return self.questions() + [
            {
                "key": "agree",
                "description": "The portal is easy to use.",
                "question_type": checklist_enums.QuestionTypes.LIKERT,
                "likert_scale_length": 5,
                "likert_low_label": "Disagree",
                "likert_high_label": "Agree",
            },
            {
                "key": "features",
                "description": "Which features do you use?",
                "question_type": checklist_enums.QuestionTypes.MULTI_SELECT,
                "options": [
                    {"key": "opt-vm", "label": "VMs"},
                    {"key": "opt-storage", "label": "Storage"},
                ],
            },
            {
                "key": "story",
                "description": "Tell us about your work.",
                "question_type": checklist_enums.QuestionTypes.RICH_TEXT,
                "rich_text_char_limit": 500,
                "conditions": [
                    {
                        "depends_on": "features",
                        "operator": "in",
                        "required_answer_value": ["opt-vm", "opt-storage"],
                    }
                ],
            },
            {
                "key": "comment",
                "description": "Anything else?",
                "question_type": checklist_enums.QuestionTypes.TEXT_AREA,
            },
        ]

    def create_survey(self, questions=None):
        response = self.create(questions=questions or self.questions())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return models.FeedbackSurvey.objects.get(uuid=response.data["uuid"])

    def update(self, survey, method="patch", **data):
        self.client.force_authenticate(self.fixture.staff)
        return getattr(self.client, method)(
            factories.FeedbackSurveyFactory.get_url(survey), data, format="json"
        )

    def shape(self, checklist):
        """The question set without its uuids, conditions by text and label."""
        result = []
        for question in checklist.get_questions():
            conditions = []
            for dependency in question.dependencies.all():
                parent = dependency.depends_on_question
                value = dependency.required_answer_value
                if parent.question_options.exists():
                    labels = {
                        option.uuid.hex: option.label
                        for option in parent.question_options.all()
                    }
                    value = [labels[item] for item in value]
                conditions.append((parent.description, dependency.operator, value))
            result.append(
                (
                    question.description,
                    question.question_type,
                    [option.label for option in question.question_options.all()],
                    conditions,
                )
            )
        return result

    def test_retrieve_round_trips(self):
        survey = self.create_survey(self.round_trip_questions())
        before = self.shape(survey.checklist)
        old_uuids = set(survey.checklist.questions.values_list("uuid", flat=True))

        self.client.force_authenticate(self.fixture.staff)
        data = self.client.get(factories.FeedbackSurveyFactory.get_url(survey)).data
        self.assertEqual(len(data["questions"]), 6)
        rating = data["questions"][0]
        self.assertEqual(
            rating["key"], survey.checklist.questions.get(order=0).uuid.hex
        )
        used = data["questions"][1]
        self.assertEqual(
            rating["conditions"][0],
            {
                "depends_on": used["key"],
                "operator": "in",
                "required_answer_value": [used["options"][0]["key"]],
            },
        )

        response = self.update(
            survey,
            method="put",
            name=data["name"],
            starts_at=data["starts_at"],
            ends_at=data["ends_at"],
            audience_query={"all_users": True},
            questions=data["questions"],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        survey.refresh_from_db()
        self.assertEqual(self.shape(survey.checklist), before)
        new_uuids = set(survey.checklist.questions.values_list("uuid", flat=True))
        self.assertFalse(old_uuids & new_uuids)
        self.assertEqual(len(response.data["questions"]), 6)

    def test_retrieve_normalises_legacy_option_references(self):
        parent = checklist_factories.QuestionFactory(
            checklist=self.survey.checklist,
            question_type=checklist_enums.QuestionTypes.SINGLE_SELECT,
        )
        option = checklist_factories.QuestionOptionFactory(question=parent)
        checklist_models.QuestionDependency.objects.create(
            question=self.question,
            depends_on_question=parent,
            operator="in",
            required_answer_value=[str(uuid.UUID(option.uuid.hex))],
        )
        self.client.force_authenticate(self.fixture.staff)
        data = self.client.get(
            factories.FeedbackSurveyFactory.get_url(self.survey)
        ).data
        (dumped,) = [q for q in data["questions"] if q["key"] == self.question.uuid.hex]
        self.assertEqual(
            dumped["conditions"][0]["required_answer_value"], [option.uuid.hex]
        )

    def test_update_replaces_questions_when_unprompted(self):
        survey = self.create_survey()
        checklist = survey.checklist
        response = self.update(
            survey,
            questions=[
                {
                    "key": "new",
                    "description": "What should we improve?",
                    "question_type": checklist_enums.QuestionTypes.TEXT_AREA,
                }
            ],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        survey.refresh_from_db()
        self.assertEqual(survey.checklist, checklist)
        self.assertEqual(
            [q.description for q in checklist.get_questions()],
            ["What should we improve?"],
        )
        self.assertFalse(
            checklist_models.QuestionOption.objects.filter(
                question__checklist=checklist
            ).exists()
        )
        self.assertEqual(
            [q["description"] for q in response.data["questions"]],
            ["What should we improve?"],
        )

    def test_partial_update_still_checks_each_question(self):
        survey = self.create_survey()
        response = self.update(
            survey,
            questions=[
                {
                    "description": "No key",
                    "question_type": checklist_enums.QuestionTypes.TEXT_AREA,
                }
            ],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("key", response.data["questions"][0])
        self.assertEqual(survey.checklist.questions.count(), 2)

    def test_update_refuses_empty_questions(self):
        survey = self.create_survey()
        response = self.update(survey, questions=[])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(survey.checklist.questions.count(), 2)

    def test_update_refuses_questions_when_prompted(self):
        self.prompt()
        response = self.update(self.survey, questions=self.questions())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["questions"],
            ["The questionnaire can't be changed once users have been prompted."],
        )
        self.assertEqual(self.survey.checklist.questions.get(), self.question)

        response = self.update(self.survey, name="Renamed")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.name, "Renamed")

    def test_put_without_questions_keeps_them(self):
        survey = self.create_survey()
        before = self.shape(survey.checklist)
        response = self.update(
            survey,
            method="put",
            name="Renamed",
            starts_at="2026-01-01T00:00:00Z",
            ends_at="2026-02-01T00:00:00Z",
            audience_query={"all_users": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        survey.refresh_from_db()
        self.assertEqual(survey.name, "Renamed")
        self.assertEqual(self.shape(survey.checklist), before)

    def test_failed_replace_keeps_old_questions(self):
        survey = self.create_survey()
        before = self.shape(survey.checklist)
        with mock.patch(
            "waldur_mastermind.feedback.questions.create_question_set",
            side_effect=serializers.ValidationError("boom"),
        ):
            response = self.update(survey, questions=self.questions())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.shape(survey.checklist), before)

    def test_list_has_no_questions_field(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.FeedbackSurveyFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("questions", response.data[0])


class SurveyDeleteTest(FeedbackTestMixin, test.APITestCase):
    def delete(self, survey):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.delete(factories.FeedbackSurveyFactory.get_url(survey))

    def test_delete_removes_owned_checklist(self):
        checklist = self.survey.checklist
        self.assertEqual(
            self.delete(self.survey).status_code, status.HTTP_204_NO_CONTENT
        )
        self.assertFalse(
            checklist_models.Checklist.objects.filter(pk=checklist.pk).exists()
        )
        self.assertFalse(
            checklist_models.Question.objects.filter(pk=self.question.pk).exists()
        )

    def test_delete_keeps_checklist_still_referenced_elsewhere(self):
        checklist = self.survey.checklist
        step = proposal_factories.CallWorkflowStepFactory()
        step.checklist = checklist
        step.save(update_fields=["checklist"])

        self.assertEqual(
            self.delete(self.survey).status_code, status.HTTP_204_NO_CONTENT
        )
        self.assertFalse(models.FeedbackSurvey.objects.filter(pk=self.survey.pk))
        self.assertTrue(
            checklist_models.Checklist.objects.filter(pk=checklist.pk).exists()
        )

    def test_delete_prompted_survey_removes_its_answers(self):
        self.prompt()
        request = models.FeedbackRequest.objects.get(user=self.user)
        checklist_factories.AnswerFactory(
            completion=request.checklist_completion,
            question=self.question,
            user=self.user,
            answer_data=8,
        )

        self.assertEqual(
            self.delete(self.survey).status_code, status.HTTP_204_NO_CONTENT
        )
        self.assertFalse(models.FeedbackRequest.objects.exists())
        self.assertFalse(checklist_models.ChecklistCompletion.objects.exists())
        self.assertFalse(checklist_models.Answer.objects.exists())
        self.assertFalse(
            checklist_models.Checklist.objects.filter(
                pk=self.question.checklist_id
            ).exists()
        )


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


class ChecklistAdminLockTest(FeedbackTestMixin, test.APITestCase):
    def edit(self, checklist):
        """Status codes of one question create, question patch, option create
        and dependency delete on ``checklist`` through the checklists admin."""
        select = checklist_factories.QuestionFactory(
            checklist=checklist,
            question_type=checklist_enums.QuestionTypes.SINGLE_SELECT,
        )
        dependent = checklist_factories.QuestionFactory(checklist=checklist)
        dependency = checklist_factories.QuestionDependencyFactory(
            question=dependent, depends_on_question=select
        )
        self.client.force_authenticate(self.fixture.staff)
        responses = [
            self.client.post(
                checklist_factories.QuestionFactory.get_admin_list_url(),
                {
                    "description": "New question",
                    "checklist": checklist_factories.ChecklistFactory.get_admin_url(
                        checklist
                    ),
                    "question_type": checklist_enums.QuestionTypes.TEXT_INPUT,
                },
            ),
            self.client.patch(
                checklist_factories.QuestionFactory.get_admin_url(select),
                {"description": "Changed question"},
            ),
            self.client.post(
                checklist_factories.QuestionOptionFactory.get_admin_list_url(),
                {
                    "question": checklist_factories.QuestionFactory.get_admin_url(
                        select
                    ),
                    "label": "New option",
                    "order": 1,
                },
            ),
            self.client.delete(
                checklist_factories.QuestionDependencyFactory.get_admin_url(dependency)
            ),
        ]
        return responses

    def test_prompted_survey_checklist_refuses_edits(self):
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)

        for response in self.edit(self.survey.checklist):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data, [utils.QUESTIONNAIRE_LOCKED])

    def rename_and_delete(self, checklist):
        self.client.force_authenticate(self.fixture.staff)
        url = checklist_factories.ChecklistFactory.get_admin_url(checklist)
        return (
            self.client.patch(url, {"name": "Renamed"}),
            self.client.delete(url),
        )

    def test_prompted_survey_checklist_cannot_be_renamed_or_deleted(self):
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        name = self.survey.checklist.name

        for response in self.rename_and_delete(self.survey.checklist):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data, [utils.QUESTIONNAIRE_LOCKED])
        self.survey.checklist.refresh_from_db()
        self.assertEqual(self.survey.checklist.name, name)

    def test_survey_checklist_is_deleted_with_the_survey_only(self):
        rename, delete = self.rename_and_delete(self.survey.checklist)
        self.assertEqual(rename.status_code, status.HTTP_200_OK, rename.data)
        self.assertEqual(delete.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            delete.data,
            {"detail": "Cannot delete checklist with existing feedback surveys"},
        )
        self.assertTrue(
            checklist_models.Checklist.objects.filter(
                pk=self.survey.checklist.pk
            ).exists()
        )

    def test_other_checklist_can_be_renamed_and_deleted_while_a_survey_is_prompted(
        self,
    ):
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        rename, delete = self.rename_and_delete(checklist_factories.ChecklistFactory())
        self.assertEqual(rename.status_code, status.HTTP_200_OK, rename.data)
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)

    def test_unprompted_survey_checklist_accepts_edits(self):
        self.assertEqual(
            [response.status_code for response in self.edit(self.survey.checklist)],
            [
                status.HTTP_201_CREATED,
                status.HTTP_200_OK,
                status.HTTP_201_CREATED,
                status.HTTP_204_NO_CONTENT,
            ],
        )

    def test_other_checklist_accepts_edits_while_a_survey_is_prompted(self):
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        checklist = checklist_factories.ChecklistFactory()

        self.assertEqual(
            [response.status_code for response in self.edit(checklist)],
            [
                status.HTTP_201_CREATED,
                status.HTTP_200_OK,
                status.HTTP_201_CREATED,
                status.HTTP_204_NO_CONTENT,
            ],
        )

    def test_question_cannot_move_into_prompted_survey_checklist(self):
        self.assertEqual(self.prompt().status_code, status.HTTP_200_OK)
        question = checklist_factories.QuestionFactory()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            checklist_factories.QuestionFactory.get_admin_url(question),
            {
                "checklist": checklist_factories.ChecklistFactory.get_admin_url(
                    self.survey.checklist
                )
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, [utils.QUESTIONNAIRE_LOCKED])
