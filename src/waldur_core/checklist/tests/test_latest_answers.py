"""The latest answer per question stands for the question.

Answers are per-user rows (unique on completion, question and user), so a question
answered by several users carries several rows. Only the most recently modified row
counts: for display, visibility, review flags and completion metrics.
"""

from datetime import timedelta

from django.db.models import Max
from rest_framework import test

from waldur_core.checklist import enums, models
from waldur_core.checklist.tests import factories
from waldur_core.structure.tests import fixtures as structure_fixtures


class LatestAnswerTestMixin:
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.first_user = self.fixture.owner
        self.second_user = self.fixture.manager

        self.checklist = factories.ChecklistFactory()
        self.review_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.BOOLEAN,
            required=True,
            order=1,
            review_answer_value=True,
            operator="equals",
        )
        self.text_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.TEXT_INPUT,
            required=True,
            order=2,
        )
        self.optional_question = factories.QuestionFactory(
            checklist=self.checklist,
            question_type=enums.QuestionTypes.TEXT_INPUT,
            required=False,
            order=3,
        )
        self.completion = models.ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=self.fixture.project
        )

    def _answer(self, user, question, answer_data):
        """Write an answer the way the submit_answers endpoints do."""
        answer, _ = models.Answer.objects.update_or_create(
            completion=self.completion,
            question=question,
            user=user,
            defaults={"answer_data": answer_data},
        )
        return answer

    def _make_latest(self, answer):
        """Move ``answer`` ahead of every other row and re-derive the completion status.

        ``modified`` is ``auto_now``, so save() would overwrite it; update() does not.
        """
        newest = self.completion.answers.aggregate(Max("modified"))["modified__max"]
        models.Answer.objects.filter(pk=answer.pk).update(
            modified=newest + timedelta(hours=1)
        )
        self.completion.update_completion_status()
        self.completion.refresh_from_db()


class LatestAnswersTest(LatestAnswerTestMixin, test.APITestCase):
    def test_newest_row_per_question_wins(self):
        first = self._answer(self.first_user, self.text_question, "first")
        second = self._answer(self.second_user, self.text_question, "second")
        self.assertEqual(
            self.completion.get_latest_answers()[self.text_question.id], second
        )

        self._make_latest(first)
        self.assertEqual(
            self.completion.get_latest_answers()[self.text_question.id], first
        )

    def test_tie_on_modified_goes_to_higher_id(self):
        first = self._answer(self.first_user, self.text_question, "first")
        second = self._answer(self.second_user, self.text_question, "second")
        models.Answer.objects.filter(pk__in=[first.pk, second.pk]).update(
            modified=first.modified
        )
        self.assertEqual(
            self.completion.get_latest_answers()[self.text_question.id], second
        )

    def test_scoped_to_one_user(self):
        first = self._answer(self.first_user, self.text_question, "first")
        self._answer(self.second_user, self.text_question, "second")
        self.assertEqual(
            self.completion.get_latest_answers(user=self.first_user),
            {self.text_question.id: first},
        )


class AnswerReviewFlagTest(LatestAnswerTestMixin, test.APITestCase):
    def test_editing_an_answer_recomputes_its_review_flag(self):
        answer = self._answer(self.first_user, self.review_question, True)
        answer.refresh_from_db()
        self.assertTrue(answer.requires_review)

        answer = self._answer(self.first_user, self.review_question, False)
        answer.refresh_from_db()
        self.assertFalse(answer.requires_review)

        answer = self._answer(self.first_user, self.review_question, True)
        answer.refresh_from_db()
        self.assertTrue(answer.requires_review)


class CompletionFollowsLatestAnswerTest(LatestAnswerTestMixin, test.APITestCase):
    def test_superseded_trigger_does_not_require_review(self):
        triggering = self._answer(self.first_user, self.review_question, True)
        self._answer(self.second_user, self.review_question, False)
        self.completion.refresh_from_db()
        self.assertFalse(self.completion.requires_review)

        self._make_latest(triggering)
        self.assertTrue(self.completion.requires_review)

    def test_review_summary_lists_each_triggering_question_once(self):
        self._answer(self.first_user, self.review_question, True)
        self._answer(self.second_user, self.review_question, True)

        self.assertEqual(len(self.completion.get_review_trigger_summary()), 1)
        self.assertEqual(
            list(self.completion.get_questions_requiring_review()),
            [self.review_question.id],
        )

    def test_review_summary_skips_superseded_trigger(self):
        self._answer(self.first_user, self.review_question, True)
        self._answer(self.second_user, self.review_question, False)

        self.assertEqual(self.completion.get_review_trigger_summary(), [])
        self.assertEqual(list(self.completion.get_questions_requiring_review()), [])

    def test_completion_percentage_counts_questions_not_rows(self):
        self._answer(self.first_user, self.text_question, "first")
        self._answer(self.second_user, self.text_question, "second")

        self.assertEqual(self.completion.get_completion_percentage(), 33.3)

    def test_completion_percentage_never_exceeds_hundred(self):
        for user in (self.first_user, self.second_user):
            self._answer(user, self.review_question, False)
            self._answer(user, self.text_question, "text")
            self._answer(user, self.optional_question, "text")

        self.assertEqual(self.completion.get_completion_percentage(), 100.0)

    def test_required_questions_answered_by_different_users_complete(self):
        self._answer(self.first_user, self.review_question, False)
        self._answer(self.second_user, self.text_question, "text")
        self.completion.refresh_from_db()

        self.assertTrue(self.completion.is_completed)
