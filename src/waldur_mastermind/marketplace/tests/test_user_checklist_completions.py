from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status, test

from waldur_core.checklist import models as checklist_models
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class OfferingUserChecklistCompletionsViewSetTest(test.APITestCase):
    """Test listing checklist completions for user's offering users."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.user = self.fixture.user

        # Create additional offerings with different checklists
        self.offering1 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Test Offering 1",
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        self.checklist1 = checklist_factories.ChecklistFactory(
            name="Compliance Checklist 1"
        )
        self.offering1.compliance_checklist = self.checklist1
        self.offering1.save()

        self.offering2 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Test Offering 2",
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        self.checklist2 = checklist_factories.ChecklistFactory(
            name="Compliance Checklist 2"
        )
        self.offering2.compliance_checklist = self.checklist2
        self.offering2.save()

        # Create offering without checklist
        self.offering3 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Test Offering 3 (No Checklist)",
            plugin_options={"service_provider_can_create_offering_user": True},
        )

        # Create OfferingUsers for the test user
        self.offering_user1 = factories.OfferingUserFactory(
            offering=self.offering1, user=self.user, username="test_user1"
        )
        self.offering_user2 = factories.OfferingUserFactory(
            offering=self.offering2, user=self.user, username="test_user2"
        )
        self.offering_user3 = factories.OfferingUserFactory(
            offering=self.offering3, user=self.user, username="test_user3"
        )

        # Get or update completion for offering_user1 (handler may have created it)
        content_type = ContentType.objects.get_for_model(models.OfferingUser)
        self.completion1, _ = (
            checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=self.checklist1,
                scope_content_type=content_type,
                scope_object_id=self.offering_user1.id,
                defaults={"is_completed": False},
            )
        )
        self.completion1.is_completed = False
        self.completion1.save()

        # Get or update completion for offering_user2 (completed)
        self.completion2, _ = (
            checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=self.checklist2,
                scope_content_type=content_type,
                scope_object_id=self.offering_user2.id,
                defaults={"is_completed": True},
            )
        )
        self.completion2.is_completed = True
        self.completion2.save()

        # No completion for offering_user3 (no checklist configured)

        self.url = reverse("marketplace-offering-user-checklist-completion-list")

    def test_user_can_list_own_checklist_completions(self):
        """Test that users can list their own checklist completions."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Response is a list of checklist completions
        results = response.data
        self.assertEqual(len(results), 2)  # Only 2 with completions

        # Check results - should contain completions, not offering users without completions
        self.assertEqual(len(results), 2)

        # Check the results contain expected data
        offering_user_uuids = [r["offering_user_uuid"] for r in results]
        self.assertIn(str(self.offering_user1.uuid), offering_user_uuids)
        self.assertIn(str(self.offering_user2.uuid), offering_user_uuids)
        # offering_user3 should not be included since it has no checklist completion

        # Check specific completion data
        for result in results:
            self.assertIn("offering_name", result)
            self.assertIn("checklist_name", result)
            self.assertIn("is_completed", result)
            self.assertIn("completion_percentage", result)

    def test_user_cannot_see_other_users_completions(self):
        """Test that users cannot see other users' checklist completions."""
        # Create another user with offering users
        other_user = structure_fixtures.UserFixture().user
        other_offering_user = factories.OfferingUserFactory(
            offering=self.offering1, user=other_user, username="other_user"
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        # Should only see own offering users (2 completions, not 3)
        self.assertEqual(len(response.data), 2)
        offering_user_uuids = [r["offering_user_uuid"] for r in response.data]
        self.assertNotIn(str(other_offering_user.uuid), offering_user_uuids)

    def test_pagination_works_correctly(self):
        """Test that pagination works for the endpoint."""
        # Create more offering users with checklists to trigger pagination
        for i in range(15):
            offering = factories.OfferingFactory(
                customer=self.fixture.customer,
                name=f"Additional Offering {i}",
                plugin_options={"service_provider_can_create_offering_user": True},
            )
            checklist = checklist_factories.ChecklistFactory(name=f"Checklist {i}")
            offering.compliance_checklist = checklist
            offering.save()

            offering_user = factories.OfferingUserFactory(
                offering=offering, user=self.user, username=f"user_{i}"
            )

            # Create completion for each new offering user
            content_type = ContentType.objects.get_for_model(models.OfferingUser)
            checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id=offering_user.id,
                defaults={"is_completed": False},
            )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url, {"page_size": 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # With LinkHeaderPagination, count is in the header, not response data
        self.assertEqual(int(response["X-Result-Count"]), 17)  # 2 original + 15 new
        self.assertEqual(len(response.data), 10)  # page_size
        self.assertIn("Link", response)

    def test_ordering_by_last_updated(self):
        """Test that results can be ordered by last_updated."""
        self.client.force_authenticate(self.user)

        # Test descending order (default)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Test ascending order
        response = self.client.get(self.url, {"ordering": "last_updated"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_completion_percentage_calculation(self):
        """Two questions in the checklist, one answered → 50.0%."""
        question1 = checklist_factories.QuestionFactory(
            checklist=self.checklist1, description="Question 1", required=True
        )
        checklist_factories.QuestionFactory(
            checklist=self.checklist1, description="Question 2", required=False
        )

        checklist_factories.AnswerFactory(
            completion=self.completion1, question=question1, answer_data="Answer 1"
        )

        self.completion1.update_completion_status()

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        result = next(
            (
                r
                for r in response.data
                if r["offering_user_uuid"] == str(self.offering_user1.uuid)
            ),
            None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["completion_percentage"], 50.0)

    def test_completion_percentage_with_few_answers(self):
        """Regression: 10 questions, 1 answered → 10.0%.

        Previously the denominator counted Answer rows rather than checklist
        questions, so a single answer yielded ``1/1 == 100%``.
        """
        questions = [
            checklist_factories.QuestionFactory(
                checklist=self.checklist1,
                description=f"Question {i}",
                required=(i == 0),
            )
            for i in range(10)
        ]
        checklist_factories.AnswerFactory(
            completion=self.completion1,
            question=questions[0],
            answer_data="Answer 1",
        )
        self.completion1.update_completion_status()

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        result = next(
            (
                r
                for r in response.data
                if r["offering_user_uuid"] == str(self.offering_user1.uuid)
            ),
            None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["completion_percentage"], 10.0)

    def test_completion_percentage_retrieve_path(self):
        """Same scenario as above but via the single-object retrieve action.

        The retrieve path doesn't get the queryset annotations, so it exercises
        the fallback branch that delegates to ``ChecklistCompletion``'s model
        method. Both paths must agree.
        """
        questions = [
            checklist_factories.QuestionFactory(
                checklist=self.checklist1,
                description=f"Question {i}",
                required=(i == 0),
            )
            for i in range(10)
        ]
        checklist_factories.AnswerFactory(
            completion=self.completion1,
            question=questions[0],
            answer_data="Answer 1",
        )
        self.completion1.update_completion_status()

        detail_url = reverse(
            "marketplace-offering-user-checklist-completion-detail",
            kwargs={"pk": self.completion1.pk},
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["completion_percentage"], 10.0)

    def test_unauthenticated_access_denied(self):
        """Test that unauthenticated users cannot access the endpoint."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_offering_without_checklist_questions_shows_full_percentage(self):
        """A checklist with zero questions is vacuously 100% — there's nothing to answer.

        ``is_completed`` is decoupled from the percentage: the auto-created
        completion remains ``is_completed=False`` until ``update_completion_status``
        runs, but the percentage reflects the (empty) work-to-do.
        """
        offering4 = factories.OfferingFactory(
            customer=self.fixture.customer,
            name="Test Offering 4",
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        checklist4 = checklist_factories.ChecklistFactory(name="Checklist 4")
        offering4.compliance_checklist = checklist4
        offering4.save()

        offering_user4 = factories.OfferingUserFactory(
            offering=offering4, user=self.user, username="test_user4"
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        result = next(
            (
                r
                for r in response.data
                if r["offering_user_uuid"] == str(offering_user4.uuid)
            ),
            None,
        )
        self.assertIsNotNone(result)
        self.assertFalse(result["is_completed"])
        self.assertEqual(result["completion_percentage"], 100)

    def test_filter_by_user_uuid(self):
        """Test filtering completions by user UUID."""
        # Create another user with completions
        other_user = structure_fixtures.UserFixture().user
        other_offering_user = factories.OfferingUserFactory(
            offering=self.offering1, user=other_user, username="other_test_user"
        )

        # Create completion for the other user
        content_type = ContentType.objects.get_for_model(models.OfferingUser)
        checklist_models.ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist1,
            scope_content_type=content_type,
            scope_object_id=other_offering_user.id,
            defaults={"is_completed": True},
        )

        self.client.force_authenticate(self.user)

        # Test filtering by the original user UUID - should see 2 completions
        response = self.client.get(self.url, {"user_uuid": str(self.user.uuid)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # Test filtering by the other user UUID - regular user should see 0 completions
        # (regular users cannot see other users' completions due to permission restrictions)
        response = self.client.get(self.url, {"user_uuid": str(other_user.uuid)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        # Test that staff user can see other user's completions
        staff_user = structure_fixtures.UserFixture().user
        staff_user.is_staff = True
        staff_user.save()
        self.client.force_authenticate(staff_user)

        response = self.client.get(self.url, {"user_uuid": str(other_user.uuid)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["offering_user_uuid"], str(other_offering_user.uuid)
        )

    def test_filter_by_offering_uuid(self):
        """Test filtering completions by offering UUID."""
        self.client.force_authenticate(self.user)

        # Test filtering by offering1 UUID - should see 1 completion
        response = self.client.get(
            self.url, {"offering_uuid": str(self.offering1.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["offering_name"], "Test Offering 1")

        # Test filtering by offering2 UUID - should see 1 completion
        response = self.client.get(
            self.url, {"offering_uuid": str(self.offering2.uuid)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["offering_name"], "Test Offering 2")

    def test_filter_by_completion_status(self):
        """Test filtering completions by completion status."""
        self.client.force_authenticate(self.user)

        # Test filtering by completed=True - should see 1 completion
        response = self.client.get(self.url, {"is_completed": "true"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]["is_completed"])

        # Test filtering by completed=False - should see 1 completion
        response = self.client.get(self.url, {"is_completed": "false"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertFalse(response.data[0]["is_completed"])

    def test_staff_can_see_all_completions(self):
        """Test that staff users can see all completions regardless of ownership."""
        # Create a staff user
        staff_user = structure_fixtures.UserFixture().user
        staff_user.is_staff = True
        staff_user.save()

        # Create completions for another user
        other_user = structure_fixtures.UserFixture().user
        other_offering_user = factories.OfferingUserFactory(
            offering=self.offering1, user=other_user, username="other_test_user"
        )

        content_type = ContentType.objects.get_for_model(models.OfferingUser)
        checklist_models.ChecklistCompletion.objects.get_or_create(
            checklist=self.checklist1,
            scope_content_type=content_type,
            scope_object_id=other_offering_user.id,
            defaults={"is_completed": True},
        )

        self.client.force_authenticate(staff_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Staff should see all completions (original 2 + 1 new = 3)
        self.assertEqual(len(response.data), 3)

    def test_service_provider_can_see_managed_offerings_completions(self):
        """Test that service providers can see completions for their managed offerings."""
        from waldur_core.permissions.fixtures import CustomerRole

        # Create a service provider user with permission to the customer
        sp_user = structure_fixtures.UserFixture().user
        self.fixture.customer.add_user(sp_user, CustomerRole.SUPPORT)

        self.client.force_authenticate(sp_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Service provider should see completions for offerings they manage
        self.assertGreaterEqual(len(response.data), 2)

    def test_empty_results_when_no_completions_exist(self):
        """Test that endpoint returns empty list when no completions exist."""
        # Create a new user with no completions
        new_user = structure_fixtures.UserFixture().user

        self.client.force_authenticate(new_user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_ordering_by_different_fields(self):
        """Test ordering by different available fields."""
        self.client.force_authenticate(self.user)

        # Test ordering by is_completed ascending
        response = self.client.get(self.url, {"o": "is_completed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Test ordering by is_completed descending
        response = self.client.get(self.url, {"o": "-is_completed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Test ordering by modified ascending
        response = self.client.get(self.url, {"o": "modified"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filter_combinations(self):
        """Test combining multiple filters."""
        self.client.force_authenticate(self.user)

        # Test combining user_uuid and offering_uuid filters
        response = self.client.get(
            self.url,
            {
                "user_uuid": str(self.user.uuid),
                "offering_uuid": str(self.offering1.uuid),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Test combining user_uuid and is_completed filters
        response = self.client.get(
            self.url, {"user_uuid": str(self.user.uuid), "is_completed": "false"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_uuid_filters(self):
        """Test behavior with invalid UUID filters."""
        self.client.force_authenticate(self.user)

        # Test with invalid user UUID
        response = self.client.get(self.url, {"user_uuid": "invalid-uuid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Test with invalid offering UUID
        response = self.client.get(self.url, {"offering_uuid": "invalid-uuid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nonexistent_uuid_filters(self):
        """Test behavior with nonexistent but valid UUIDs."""
        import uuid

        self.client.force_authenticate(self.user)

        # Test with nonexistent user UUID
        response = self.client.get(self.url, {"user_uuid": str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        # Test with nonexistent offering UUID
        response = self.client.get(self.url, {"offering_uuid": str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_serializer_method_fields_return_correct_types(self):
        """Test that serializer method fields return expected data types."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)

        result = response.data[0]

        # Test offering_user_uuid is a string
        self.assertIsInstance(result["offering_user_uuid"], str)

        # Test offering_name is a string
        self.assertIsInstance(result["offering_name"], str)

        # Test offering_uuid is a string
        self.assertIsInstance(result["offering_uuid"], str)

        # Test completion_percentage is a number
        self.assertIsInstance(result["completion_percentage"], (int, float))
        self.assertGreaterEqual(result["completion_percentage"], 0)
        self.assertLessEqual(result["completion_percentage"], 100)

        # Test unanswered_required_questions is an integer
        self.assertIsInstance(result["unanswered_required_questions"], int)
        self.assertGreaterEqual(result["unanswered_required_questions"], 0)

    def test_viewset_uses_correct_base_class(self):
        """Test that the ViewSet uses ReadOnlyActionsViewSet."""
        from waldur_core.core.views import ReadOnlyActionsViewSet
        from waldur_mastermind.marketplace.views import (
            OfferingUserChecklistCompletionsViewSet,
        )

        self.assertTrue(
            issubclass(OfferingUserChecklistCompletionsViewSet, ReadOnlyActionsViewSet)
        )

    def test_required_question_without_answer_counts_as_unanswered(self):
        """A required question with NO Answer row counts toward unanswered_required_questions."""
        checklist_factories.QuestionFactory(
            checklist=self.checklist1, description="Required Q", required=True
        )
        checklist_factories.QuestionFactory(
            checklist=self.checklist1, description="Optional Q", required=False
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = next(
            r
            for r in response.data
            if r["offering_user_uuid"] == str(self.offering_user1.uuid)
        )
        self.assertEqual(result["unanswered_required_questions"], 1)

    def test_required_question_with_answer_row_counts_as_answered(self):
        """An existing Answer row marks the required question as answered."""
        question = checklist_factories.QuestionFactory(
            checklist=self.checklist1, description="Required Q", required=True
        )
        checklist_factories.AnswerFactory(
            user=self.user,
            question=question,
            completion=self.completion1,
            answer_data=["yes"],
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = next(
            r
            for r in response.data
            if r["offering_user_uuid"] == str(self.offering_user1.uuid)
        )
        self.assertEqual(result["unanswered_required_questions"], 0)

    def test_list_query_count_does_not_scale_per_completion(self):
        """List endpoint must bulk-fetch OfferingUsers; query count is bounded."""
        # Add several more completions to amplify any N+1.
        content_type = ContentType.objects.get_for_model(models.OfferingUser)
        for i in range(5):
            offering = factories.OfferingFactory(
                customer=self.fixture.customer,
                name=f"Bulk Offering {i}",
                plugin_options={"service_provider_can_create_offering_user": True},
            )
            checklist = checklist_factories.ChecklistFactory(name=f"Bulk Checklist {i}")
            offering.compliance_checklist = checklist
            offering.save()
            offering_user = factories.OfferingUserFactory(
                offering=offering, user=self.user, username=f"bulk_user_{i}"
            )
            checklist_models.ChecklistCompletion.objects.get_or_create(
                checklist=checklist,
                scope_content_type=content_type,
                scope_object_id=offering_user.id,
                defaults={"is_completed": False},
            )

        self.client.force_authenticate(self.user)
        # Warm up: hit the endpoint once so any per-request caches (e.g.
        # ContentType) are populated and don't skew the query count.
        self.client.get(self.url)

        # Pinned to lock in the bulk-fetch optimization. Should NOT grow as
        # more completions are added — verify by re-running with 50 vs. 5.
        with self.assertNumQueries(7):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data), 7)

    def test_create_update_delete_operations_disabled(self):
        """Test that create, update, and delete operations are disabled."""
        self.client.force_authenticate(self.user)

        # Test POST (create) - should not be allowed
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        # Test PUT (update) - should not be allowed
        if len(self.client.get(self.url).data) > 0:
            completion_uuid = self.client.get(self.url).data[0]["uuid"]
            response = self.client.put(f"{self.url}{completion_uuid}/", {})
            self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        # Test DELETE - should not be allowed
        if len(self.client.get(self.url).data) > 0:
            completion_uuid = self.client.get(self.url).data[0]["uuid"]
            response = self.client.delete(f"{self.url}{completion_uuid}/")
            self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
