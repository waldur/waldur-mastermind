from rest_framework import test

from waldur_core.checklist.enums import DependencyLogicOperators
from waldur_core.checklist.models import (
    Answer,
    ChecklistCompletion,
    Question,
    QuestionDependency,
)
from waldur_core.checklist.tests.fixtures import CheckListFixture
from waldur_core.structure.tests.factories import UserFactory
from waldur_core.structure.tests.fixtures import ProjectFixture


class DependencyLogicOperatorTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = CheckListFixture()
        self.user = UserFactory()
        self.project_fixture = ProjectFixture()

        # Create completion for testing
        self.completion = ChecklistCompletion.objects.create(
            checklist=self.fixture.checklist,
            scope=self.project_fixture.project,
        )

    def test_default_and_logic_behavior(self):
        """Test that questions default to AND logic and work correctly."""
        # Create dependency questions
        question_a = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Question A",
            question_type="boolean",
            order=1,
        )
        question_b = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Question B",
            question_type="boolean",
            order=2,
        )

        # Create target question with default AND logic
        target_question = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Target Question (AND default)",
            question_type="text_input",
            order=3,
        )

        # Verify default value
        self.assertEqual(
            target_question.dependency_logic_operator, DependencyLogicOperators.AND
        )

        # Create dependencies: target_question depends on A=True AND B=True
        QuestionDependency.objects.create(
            question=target_question,
            depends_on_question=question_a,
            required_answer_value=True,
            operator="equals",
        )
        QuestionDependency.objects.create(
            question=target_question,
            depends_on_question=question_b,
            required_answer_value=True,
            operator="equals",
        )

        # Test: No answers - should not be visible
        self.assertFalse(target_question.is_visible_for_completion(self.completion))

        # Test: Only A answered - should not be visible (AND logic)
        Answer.objects.create(
            question=question_a,
            user=self.user,
            completion=self.completion,
            answer_data=True,
        )
        self.assertFalse(target_question.is_visible_for_completion(self.completion))

        # Test: A=True, B=True - should be visible
        Answer.objects.create(
            question=question_b,
            user=self.user,
            completion=self.completion,
            answer_data=True,
        )
        self.assertTrue(target_question.is_visible_for_completion(self.completion))

    def test_explicit_or_logic_behavior(self):
        """Test that questions with OR logic work correctly."""
        # Create dependency questions
        question_a = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Question A",
            question_type="boolean",
            order=1,
        )
        question_b = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Question B",
            question_type="boolean",
            order=2,
        )

        # Create target question with OR logic
        target_question = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Target Question (OR)",
            question_type="text_input",
            order=3,
            dependency_logic_operator=DependencyLogicOperators.OR,
        )

        # Create dependencies: target_question depends on A=True OR B=True
        QuestionDependency.objects.create(
            question=target_question,
            depends_on_question=question_a,
            required_answer_value=True,
            operator="equals",
        )
        QuestionDependency.objects.create(
            question=target_question,
            depends_on_question=question_b,
            required_answer_value=True,
            operator="equals",
        )

        # Test: No answers - should not be visible
        self.assertFalse(target_question.is_visible_for_completion(self.completion))

        # Test: Only A answered - should be visible (OR logic)
        Answer.objects.create(
            question=question_a,
            user=self.user,
            completion=self.completion,
            answer_data=True,
        )
        self.assertTrue(target_question.is_visible_for_completion(self.completion))

    def test_or_logic_with_mixed_conditions(self):
        """Test OR logic where one condition is met and another is not."""
        # Create dependency questions
        question_a = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Question A",
            question_type="boolean",
            order=1,
        )
        question_b = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Question B",
            question_type="boolean",
            order=2,
        )

        # Create target question with OR logic
        target_question = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Target Question (OR mixed)",
            question_type="text_input",
            order=3,
            dependency_logic_operator=DependencyLogicOperators.OR,
        )

        # Create dependencies: target_question depends on A=True OR B=True
        QuestionDependency.objects.create(
            question=target_question,
            depends_on_question=question_a,
            required_answer_value=True,
            operator="equals",
        )
        QuestionDependency.objects.create(
            question=target_question,
            depends_on_question=question_b,
            required_answer_value=True,
            operator="equals",
        )

        # Test: A=True, B=False - should be visible (OR logic, one condition met)
        Answer.objects.create(
            question=question_a,
            user=self.user,
            completion=self.completion,
            answer_data=True,
        )
        Answer.objects.create(
            question=question_b,
            user=self.user,
            completion=self.completion,
            answer_data=False,
        )
        self.assertTrue(target_question.is_visible_for_completion(self.completion))

    def test_no_dependencies_always_visible(self):
        """Test that questions without dependencies are always visible regardless of logic operator."""
        # Test with AND logic
        question_and = Question.objects.create(
            checklist=self.fixture.checklist,
            description="No deps AND",
            question_type="text_input",
            order=1,
            dependency_logic_operator=DependencyLogicOperators.AND,
        )

        # Test with OR logic
        question_or = Question.objects.create(
            checklist=self.fixture.checklist,
            description="No deps OR",
            question_type="text_input",
            order=2,
            dependency_logic_operator=DependencyLogicOperators.OR,
        )

        # Both should be visible
        self.assertTrue(question_and.is_visible_for_completion(self.completion))
        self.assertTrue(question_or.is_visible_for_completion(self.completion))

    def test_multi_user_answers_visibility(self):
        """Test that questions are visible based on answers from any user in the completion context."""
        # Create dependency questions
        question_a = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Question A (answered by user1)",
            question_type="boolean",
            order=1,
        )
        question_b = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Question B (answered by user2)",
            question_type="text_input",
            order=2,
        )

        # Create target question with OR logic
        target_question = Question.objects.create(
            checklist=self.fixture.checklist,
            description="Target Question (depends on A OR B)",
            question_type="text_input",
            order=3,
            dependency_logic_operator=DependencyLogicOperators.OR,
        )

        # Create dependencies: target depends on A=True OR B contains "test"
        QuestionDependency.objects.create(
            question=target_question,
            depends_on_question=question_a,
            required_answer_value=True,
            operator="equals",
        )
        QuestionDependency.objects.create(
            question=target_question,
            depends_on_question=question_b,
            required_answer_value=["test"],
            operator="contains",
        )

        # Create two different users
        user1 = UserFactory()
        user2 = UserFactory()

        # Test: No answers - target should not be visible
        self.assertFalse(target_question.is_visible_for_completion(self.completion))

        # User1 answers question A with True
        Answer.objects.create(
            question=question_a,
            user=user1,
            completion=self.completion,
            answer_data=True,
        )

        # Target should now be visible (A=True satisfies OR condition)
        self.assertTrue(target_question.is_visible_for_completion(self.completion))

        # User2 answers question B with "testing"
        Answer.objects.create(
            question=question_b,
            user=user2,
            completion=self.completion,
            answer_data="testing",
        )

        # Target should still be visible (both conditions satisfied)
        self.assertTrue(target_question.is_visible_for_completion(self.completion))

        # Change user1's answer to False
        Answer.objects.filter(question=question_a, user=user1).update(answer_data=False)

        # Target should still be visible (B contains "test" satisfies OR condition)
        self.assertTrue(target_question.is_visible_for_completion(self.completion))
