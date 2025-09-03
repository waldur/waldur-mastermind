from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist import enums, models
from waldur_core.checklist.tests import factories, fixtures


@ddt
class QuestionDependencyAdminGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.url = factories.QuestionDependencyFactory.get_admin_list_url()

    @data("staff")
    def test_user_can_list_dependencies(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner")
    def test_user_cannot_list_dependencies(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class QuestionDependencyAdminCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.select_question = factories.QuestionFactory(
            checklist=self.fixture.checklist,
            question_type=enums.QuestionTypes.MULTI_SELECT,
        )
        self.dependent_question = factories.QuestionFactory(
            checklist=self.fixture.checklist,
            question_type=enums.QuestionTypes.MULTI_SELECT,
        )
        self.url = factories.QuestionDependencyFactory.get_admin_list_url()

    def _get_payload(self):
        return {
            "question": factories.QuestionFactory.get_admin_url(
                self.dependent_question
            ),
            "depends_on_question": factories.QuestionFactory.get_admin_url(
                self.select_question
            ),
            "operator": "in",
            "required_answer_value": ["ce00425f1b254887a52c06093c05e207"],
        }

    @data("staff")
    def test_user_can_create_dependency(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.QuestionDependency.objects.filter(
                question=self.dependent_question,
                depends_on_question=self.select_question,
            ).exists()
        )

    @data("owner")
    def test_user_cannot_create_dependency(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_validate_operator(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        payload = self._get_payload()
        payload["operator"] = "equals"
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_required_answer_value(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        payload = self._get_payload()
        payload["required_answer_value"] = True
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_date_question_dependency_with_string_date(self):
        """Test that date question dependencies work with string date values"""
        user = self.fixture.staff
        self.client.force_authenticate(user)

        # Create a date question
        date_question = factories.QuestionFactory(
            checklist=self.fixture.checklist,
            question_type=enums.QuestionTypes.DATE,
            description="Set a Date?",
        )

        # Create a dependent question
        dependent_question = factories.QuestionFactory(
            checklist=self.fixture.checklist,
            question_type=enums.QuestionTypes.BOOLEAN,
        )

        payload = {
            "question": factories.QuestionFactory.get_admin_url(dependent_question),
            "depends_on_question": factories.QuestionFactory.get_admin_url(
                date_question
            ),
            "operator": "equals",
            "required_answer_value": "2025-09-05",  # String date format
        }

        response = self.client.post(self.url, payload)
        self.assertEqual(
            response.status_code,
            status.HTTP_201_CREATED,
            f"Expected 201, got {response.status_code}: {response.data}",
        )

        # Verify the dependency was created
        dependency = models.QuestionDependency.objects.get(uuid=response.data["uuid"])
        self.assertEqual(dependency.required_answer_value, "2025-09-05")
        self.assertEqual(dependency.operator, "equals")

    def test_validate_circular(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)

        # Test direct self-dependency (question depends on itself)
        payload = self._get_payload()
        payload["question"] = payload["depends_on_question"]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Test circular dependency through another question
        # Create a third question
        third_question = factories.QuestionFactory(
            checklist=self.fixture.checklist,
            question_type=enums.QuestionTypes.MULTI_SELECT,
        )

        # First, create dependency: dependent_question depends on select_question
        payload = self._get_payload()
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Now try to create circular dependency: select_question depends on third_question,
        # but third_question would depend on dependent_question, creating a cycle
        payload["question"] = factories.QuestionFactory.get_admin_url(
            self.select_question
        )
        payload["depends_on_question"] = factories.QuestionFactory.get_admin_url(
            third_question
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Now try to create the dependency that would complete the cycle:
        # third_question depends on dependent_question
        payload["question"] = factories.QuestionFactory.get_admin_url(third_question)
        payload["depends_on_question"] = factories.QuestionFactory.get_admin_url(
            self.dependent_question
        )
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class QuestionDependencyAdminUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question_dependency = self.fixture.question_dependency
        self.url = factories.QuestionDependencyFactory.get_admin_url(
            self.question_dependency
        )

    def _get_payload(self):
        return {
            "question": factories.QuestionFactory.get_admin_url(
                self.question_dependency.question
            ),
            "depends_on_question": factories.QuestionFactory.get_admin_url(
                self.question_dependency.depends_on_question
            ),
            "operator": self.question_dependency.operator,
            "required_answer_value": ["second"],
        }

    @data("staff")
    def test_user_can_update_dependency(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.put(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.QuestionDependency.objects.filter(
                pk=self.question_dependency.pk,
                required_answer_value=["second"],
            ).exists()
        )

    @data("owner")
    def test_user_cannot_update_dependency(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.put(self.url, self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.QuestionDependency.objects.filter(
                pk=self.question_dependency.pk,
                required_answer_value=["second"],
            ).exists()
        )


@ddt
class QuestionDependencyAdminDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.question_dependency = self.fixture.question_dependency
        self.url = factories.QuestionDependencyFactory.get_admin_url(
            self.question_dependency
        )

    @data("staff")
    def test_user_can_delete_dependency(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.QuestionDependency.objects.filter(
                pk=self.question_dependency.pk
            ).exists()
        )

    @data("owner")
    def test_user_cannot_delete_dependency(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.QuestionDependency.objects.filter(
                pk=self.question_dependency.pk
            ).exists()
        )


class QuestionVisibilityTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CheckListFixture()
        self.user = self.fixture.user

    def test_question_visibility_with_dependencies(self):
        # Create completion for visibility testing
        from waldur_core.checklist.models import ChecklistCompletion
        from waldur_core.structure.tests.fixtures import ProjectFixture

        project_fixture = ProjectFixture()
        completion = ChecklistCompletion.objects.create(
            checklist=self.fixture.checklist,
            scope=project_fixture.project,
        )

        self.assertEqual(
            len(self.fixture.checklist.get_visible_questions(completion)), 1
        )
        factories.AnswerFactory(
            question=self.fixture.question,
            user=self.user,
            completion=completion,
            answer_data="my first answer",
        )
        self.assertEqual(
            len(self.fixture.checklist.get_visible_questions(completion)), 2
        )
