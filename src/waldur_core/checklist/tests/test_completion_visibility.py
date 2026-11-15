from importlib import import_module
from unittest import mock

from django.apps import apps
from django.test import SimpleTestCase
from rest_framework import test

from waldur_core.checklist import enums, tasks, utils
from waldur_core.checklist.models import Answer, ChecklistCompletion
from waldur_core.checklist.tests import factories
from waldur_core.structure.tests.factories import UserFactory
from waldur_core.structure.tests.fixtures import ProjectFixture

recompute_migration = import_module(
    "waldur_core.checklist.migrations.0018_recompute_completion_visible_questions"
)


class ApplyOperatorListTest(SimpleTestCase):
    def test_equals_ignores_order_of_multi_select_answers(self):
        self.assertTrue(utils.apply_operator(["a", "b"], ["b", "a"], "equals"))
        self.assertFalse(utils.apply_operator(["a"], ["a", "b"], "equals"))

    def test_not_equals_ignores_order_of_multi_select_answers(self):
        self.assertFalse(utils.apply_operator(["a", "b"], ["b", "a"], "not_equals"))
        self.assertTrue(utils.apply_operator(["a"], ["b"], "not_equals"))

    def test_unhashable_items_compare_as_lists(self):
        files = [{"name": "a.pdf"}]
        self.assertTrue(utils.apply_operator(files, [{"name": "a.pdf"}], "equals"))


class ApplyOperatorSingleValueTest(SimpleTestCase):
    """A single-value answer, such as a country, is one value, not its characters."""

    def test_in(self):
        self.assertTrue(utils.apply_operator("EE", ["EE", "FI"], "in"))
        self.assertFalse(utils.apply_operator("LV", ["EE", "FI"], "in"))

    def test_not_in(self):
        self.assertFalse(utils.apply_operator("EE", ["EE", "FI"], "not_in"))
        self.assertTrue(utils.apply_operator("LV", ["EE", "FI"], "not_in"))

    def test_multi_select_answer_is_checked_item_by_item(self):
        self.assertTrue(utils.apply_operator(["LV", "FI"], ["EE", "FI"], "in"))
        self.assertTrue(utils.apply_operator(["LV"], ["EE", "FI"], "not_in"))


class CompletionVisibilityTest(test.APITestCase):
    """Required questions hidden by their dependencies do not count."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()
        self.user = UserFactory()
        self.completion = ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=ProjectFixture().project
        )
        self.gate = self.question(question_type=enums.QuestionTypes.BOOLEAN)
        self.hidden = self.question()
        factories.QuestionDependencyFactory(
            question=self.hidden,
            depends_on_question=self.gate,
            operator="equals",
            required_answer_value=True,
        )

    def question(self, **kwargs):
        return factories.QuestionFactory(
            checklist=self.checklist, required=True, **kwargs
        )

    def answer(self, question, answer_data):
        # Answer.save() recomputes the completion, as an answer from the API does
        Answer.objects.update_or_create(
            completion=self.completion,
            question=question,
            user=self.user,
            defaults={"answer_data": answer_data},
        )
        self.completion.refresh_from_db()

    def test_hidden_required_question_is_not_required(self):
        self.answer(self.gate, False)

        self.assertTrue(self.completion.is_completed)
        self.assertFalse(self.completion.get_unanswered_required_questions().exists())
        self.assertEqual(self.completion.get_completion_percentage(), 100)

    def test_shown_required_question_must_be_answered(self):
        self.answer(self.gate, True)

        self.assertFalse(self.completion.is_completed)
        self.assertEqual(
            list(self.completion.get_unanswered_required_questions()), [self.hidden]
        )
        self.assertEqual(self.completion.get_completion_percentage(), 50)

        self.answer(self.hidden, "details")
        self.assertTrue(self.completion.is_completed)

    def test_answer_to_a_question_that_became_hidden_is_not_counted(self):
        self.answer(self.gate, True)
        self.answer(self.hidden, "details")
        factories.QuestionFactory(checklist=self.checklist)  # optional, unanswered
        self.answer(self.gate, False)

        self.assertTrue(self.completion.is_completed)
        # gate answered, optional not; the hidden answer is left out
        self.assertEqual(self.completion.get_completion_percentage(), 50)

    def test_nested_dependency(self):
        nested = self.question()
        factories.QuestionDependencyFactory(
            question=nested,
            depends_on_question=self.hidden,
            operator="contains",
            required_answer_value=["yes"],
        )
        self.answer(self.gate, True)
        self.answer(self.hidden, "no")
        self.assertTrue(self.completion.is_completed)

        self.answer(self.hidden, "yes please")
        self.assertFalse(self.completion.is_completed)
        self.assertEqual(
            list(self.completion.get_unanswered_required_questions()), [nested]
        )

    def test_multi_select_not_in_hides_question(self):
        kinds = self.question(question_type=enums.QuestionTypes.MULTI_SELECT)
        none = factories.QuestionOptionFactory(question=kinds)
        health = factories.QuestionOptionFactory(question=kinds)
        details = self.question()
        factories.QuestionDependencyFactory(
            question=details,
            depends_on_question=kinds,
            operator="not_in",
            required_answer_value=[str(none.uuid)],
        )
        self.answer(self.gate, False)
        self.answer(kinds, [str(none.uuid)])
        self.assertTrue(self.completion.is_completed)

        self.answer(kinds, [str(health.uuid)])
        self.assertFalse(self.completion.is_completed)

    def test_multi_select_not_equals_compares_by_value(self):
        kinds = self.question(question_type=enums.QuestionTypes.MULTI_SELECT)
        first = factories.QuestionOptionFactory(question=kinds)
        second = factories.QuestionOptionFactory(question=kinds)
        details = self.question()
        factories.QuestionDependencyFactory(
            question=details,
            depends_on_question=kinds,
            operator="not_equals",
            required_answer_value=[str(first.uuid), str(second.uuid)],
        )
        self.answer(self.gate, False)
        self.answer(kinds, [str(second.uuid), str(first.uuid)])

        self.assertTrue(self.completion.is_completed)

    def test_question_under_a_hidden_parent_is_hidden(self):
        nested = self.question()
        factories.QuestionDependencyFactory(
            question=nested,
            depends_on_question=self.hidden,
            operator="contains",
            required_answer_value=["yes"],
        )
        self.answer(self.gate, True)
        self.answer(self.hidden, "yes please")
        self.assertFalse(self.completion.is_completed)

        # The kept answer to the now hidden question no longer shows nested
        self.answer(self.gate, False)
        self.assertTrue(self.completion.is_completed)

    def test_answer_to_a_hidden_question_does_not_require_review(self):
        self.hidden.review_answer_value = ["urgent"]
        self.hidden.operator = "contains"
        self.hidden.save()
        self.answer(self.gate, True)
        self.answer(self.hidden, "urgent")
        self.assertTrue(self.completion.requires_review)

        self.answer(self.gate, False)
        self.assertFalse(self.completion.requires_review)
        self.assertEqual(self.completion.get_questions_requiring_review(), [])

    def test_migration_recomputes_stored_status(self):
        self.hidden.review_answer_value = ["urgent"]
        self.hidden.operator = "contains"
        self.hidden.save()
        self.answer(self.gate, True)
        self.answer(self.hidden, "urgent")
        self.answer(self.gate, False)
        # As the old rule left it: the hidden question counted
        ChecklistCompletion.objects.filter(pk=self.completion.pk).update(
            is_completed=False, requires_review=True
        )

        recompute_migration.recompute_completion(apps, None)

        self.completion.refresh_from_db()
        self.assertTrue(self.completion.is_completed)
        self.assertFalse(self.completion.requires_review)

    def test_migration_keeps_a_shown_question_required(self):
        self.answer(self.gate, True)

        recompute_migration.recompute_completion(apps, None)

        self.completion.refresh_from_db()
        self.assertFalse(self.completion.is_completed)


@mock.patch.object(
    tasks.recompute_checklist_completions,
    "delay",
    side_effect=tasks.recompute_checklist_completions,
)
class RecomputeOnChecklistChangeTest(test.APITestCase):
    """Changing what a checklist asks recomputes its completions."""

    def setUp(self):
        self.checklist = factories.ChecklistFactory()
        self.gate = factories.QuestionFactory(
            checklist=self.checklist,
            required=True,
            question_type=enums.QuestionTypes.BOOLEAN,
        )
        self.details = factories.QuestionFactory(
            checklist=self.checklist, required=True
        )
        self.completion = ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=ProjectFixture().project
        )
        Answer.objects.create(
            completion=self.completion,
            question=self.gate,
            user=UserFactory(),
            answer_data=False,
        )

    def is_completed(self):
        self.completion.refresh_from_db()
        return self.completion.is_completed

    def test_adding_and_removing_a_dependency(self, delay):
        self.assertFalse(self.is_completed())
        dependency = factories.QuestionDependencyFactory(
            question=self.details,
            depends_on_question=self.gate,
            operator="equals",
            required_answer_value=True,
        )
        self.assertTrue(self.is_completed())

        dependency.delete()
        self.assertFalse(self.is_completed())

    def test_making_a_question_optional(self, delay):
        self.details.required = False
        self.details.save()
        self.assertTrue(self.is_completed())

    def test_editing_other_fields_does_not_recompute(self, delay):
        delay.reset_mock()
        self.details.description = "Renamed"
        self.details.save()
        delay.assert_not_called()

    def test_only_completions_whose_status_changes_are_written(self, delay):
        shown = ChecklistCompletion.objects.create(
            checklist=self.checklist, scope=ProjectFixture().project
        )
        Answer.objects.create(
            completion=shown,
            question=self.gate,
            user=UserFactory(),
            answer_data=True,
        )
        other_checklist = ChecklistCompletion.objects.create(
            checklist=factories.QuestionFactory(required=True).checklist,
            scope=ProjectFixture().project,
        )
        ChecklistCompletion.objects.filter(pk=self.completion.pk).update(
            review_notes="Reviewed meanwhile"
        )
        completions = (self.completion, shown, other_checklist)
        for completion in completions:
            completion.refresh_from_db()
        before = {c.pk: (c.is_completed, c.modified) for c in completions}

        factories.QuestionDependencyFactory(
            question=self.details,
            depends_on_question=self.gate,
            operator="equals",
            required_answer_value=True,
        )

        for completion in completions:
            completion.refresh_from_db()
        # The one whose required question is now hidden completes; the one that
        # still sees it, and another checklist's, stay as they were
        self.assertEqual([c.is_completed for c in completions], [True, False, False])
        self.assertEqual(self.completion.review_notes, "Reviewed meanwhile")
        for completion in completions:
            self.assertEqual(completion.modified, before[completion.pk][1])
