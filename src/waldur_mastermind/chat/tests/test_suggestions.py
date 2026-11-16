import dataclasses
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Feature
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat import deployment_profile
from waldur_mastermind.chat import suggestions as chat_suggestions
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.models import Offering
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.tests import factories as proposal_factories

ASSISTANT_ON = {
    "AI_ASSISTANT_ENABLED": True,
    "AI_ASSISTANT_ENABLED_ROLES": "all",
    "AI_ASSISTANT_API_URL": "https://example.com/stream",
    "AI_ASSISTANT_API_TOKEN": "dummy-token",
}


class SuggestionsTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(deployment_profile.invalidate_cache)
        self.fixture = ProjectFixture()
        self.owner = self.fixture.owner
        self.project = self.fixture.project  # the fixture creates it lazily

    def offered(self, user, delegated=False):
        return chat_suggestions.suggestions_for(user, delegated)


class CatalogTest(SuggestionsTestBase):
    def test_ids_are_unique(self):
        self.assertEqual(
            len(chat_suggestions.SUGGESTION_IDS),
            len(set(chat_suggestions.SUGGESTION_IDS)),
        )

    def test_offered_only_with_every_tool_permitted(self):
        # With every configuration term on and every data check passing,
        # what is left out is decided by the tool set alone.
        catalog = tuple(
            dataclasses.replace(entry, check=None) for entry in chat_suggestions.CATALOG
        )
        all_on = mock.patch.object(
            deployment_profile,
            "get_term_states",
            lambda terms: dict.fromkeys(terms, deployment_profile.TermState.ON),
        )
        callers = {
            "end user": (self.owner, False),
            "support": (structure_factories.UserFactory(is_support=True), False),
            "staff": (self.fixture.staff, False),
            "delegated staff": (self.fixture.staff, True),
        }
        with mock.patch.object(chat_suggestions, "CATALOG", catalog), all_on:
            for role, (user, delegated) in callers.items():
                with self.subTest(role=role):
                    permitted = set(get_tool_set_for_user(user, delegated))
                    offered = self.offered(user, delegated)
                    expected = [e.id for e in catalog if set(e.tools) <= permitted]
                    self.assertEqual(offered, expected)
                    for entry in catalog:
                        if entry.id in offered:
                            self.assertLessEqual(set(entry.tools), permitted)

    def test_end_user_gets_no_staff_suggestions(self):
        offered = self.offered(self.owner)
        self.assertNotIn("look_up_user", offered)
        self.assertNotIn("overdrawn_projects", offered)
        self.assertIn("look_up_user", self.offered(self.fixture.staff))


class DataConditionsTest(SuggestionsTestBase):
    def test_paused_resource_comes_first(self):
        self.assertNotIn("resource_paused", self.offered(self.owner))

        marketplace_factories.ResourceFactory(project=self.fixture.project, paused=True)

        offered = self.offered(self.owner)
        self.assertEqual(offered[0], "resource_paused")
        self.assertIn("my_resources", offered)

    def test_review_workload_only_for_reviewers(self):
        self.assertNotIn("review_workload", self.offered(self.owner))

        review = proposal_factories.ReviewFactory(reviewer=self.owner)
        review.state = proposal_models.Review.States.IN_REVIEW
        review.save()

        self.assertIn("review_workload", self.offered(self.owner))

    def test_create_vm_needs_an_openstack_offering(self):
        self.assertNotIn("create_vm", self.offered(self.owner))

        marketplace_factories.OfferingFactory(
            type="OpenStack.Instance", state=Offering.States.ACTIVE
        )
        deployment_profile.invalidate_cache()

        self.assertIn("create_vm", self.offered(self.owner))

    def test_staff_are_not_offered_other_peoples_paused_resources(self):
        marketplace_factories.ResourceFactory(paused=True)  # someone else's
        staff = self.fixture.staff

        self.assertNotIn("resource_paused", self.offered(staff))
        self.assertNotIn("my_resources", self.offered(staff))

        project = structure_factories.ProjectFactory()
        project.customer.add_user(staff, CustomerRole.OWNER)
        marketplace_factories.ResourceFactory(project=project, paused=True)
        self.assertEqual(self.offered(staff)[0], "resource_paused")

    def test_pending_order_awaiting_the_owners_approval(self):
        other = structure_factories.UserFactory()
        marketplace_factories.OrderFactory(
            project=self.project,
            created_by=other,
            state=OrderStates.PENDING_CONSUMER,
        )

        self.assertIn("pending_orders", self.offered(self.owner))

    def test_order_waiting_for_its_start_date_is_pending(self):
        marketplace_factories.OrderFactory(
            project=self.project,
            created_by=self.owner,
            state=OrderStates.PENDING_START_DATE,
        )

        self.assertIn("pending_orders", self.offered(self.owner))

    def test_no_hpc_suggestion_without_an_endpoint(self):
        offered = self.offered(self.owner)
        self.assertNotIn("hpc_jobs", offered)
        self.assertNotIn("hpc_software", offered)


class ConfigurationTest(SuggestionsTestBase):
    def test_concealed_prices_hide_invoices(self):
        self.assertIn("list_invoices", self.offered(self.owner))

        Feature.objects.create(key="marketplace.conceal_prices", value=True)
        deployment_profile.invalidate_cache()

        self.assertNotIn("list_invoices", self.offered(self.owner))

    def test_catalogue_only_portal_offers_no_ordering(self):
        marketplace_factories.OfferingFactory(
            type="OpenStack.Instance", state=Offering.States.ACTIVE
        )
        marketplace_factories.OrderFactory(
            project=self.project,
            created_by=self.owner,
            state=OrderStates.PENDING_CONSUMER,
        )
        deployment_profile.invalidate_cache()
        offered = self.offered(self.owner)
        self.assertIn("create_vm", offered)
        self.assertIn("pending_orders", offered)

        Feature.objects.create(key="marketplace.catalogue_only", value=True)
        deployment_profile.invalidate_cache()

        offered = self.offered(self.owner)
        self.assertNotIn("create_vm", offered)
        self.assertNotIn("pending_orders", offered)

    def test_call_only_portal_offers_no_review_prompts(self):
        review = proposal_factories.ReviewFactory(reviewer=self.owner)
        review.state = proposal_models.Review.States.IN_REVIEW
        review.save()
        self.assertIn("review_workload", self.offered(self.owner))

        Feature.objects.create(key="marketplace.call_only", value=True)
        deployment_profile.invalidate_cache()

        self.assertNotIn("review_workload", self.offered(self.owner))

    def test_hidden_marketplace_hides_find_service_from_end_users_only(self):
        self.assertIn("find_service", self.offered(self.owner))

        Feature.objects.create(
            key="marketplace.hide_marketplace_from_end_users", value=True
        )
        deployment_profile.invalidate_cache()

        self.assertNotIn("find_service", self.offered(self.owner))
        self.assertIn("find_service", self.offered(self.fixture.staff))

    def test_search_docs_follows_the_tool(self):
        # No corpus is loaded in tests, so the tool is not offered.
        self.assertNotIn("search_docs", self.offered(self.owner))
        with mock.patch(
            "waldur_mastermind.chat.docs.corpus.search_enabled", return_value=True
        ):
            self.assertIn("search_docs", self.offered(self.owner))


class QueryBudgetTest(SuggestionsTestBase):
    def count(self, user):
        self.offered(user)  # warm the deployment profile cache
        with CaptureQueriesContext(connection) as queries:
            self.offered(user)
        return len(queries)

    def test_queries_do_not_grow_with_organizations(self):
        few = self.count(self.owner)
        for _ in range(5):
            project = structure_factories.ProjectFactory()
            project.customer.add_user(self.owner, CustomerRole.OWNER)
            marketplace_factories.ResourceFactory(project=project)
        self.assertEqual(self.count(self.owner), few)


@override_config(**ASSISTANT_ON)
class SuggestionsEndpointTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.fixture = ProjectFixture()
        self.url = reverse("chat-suggestions")

    def test_returns_ids_in_order(self):
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = [item["id"] for item in response.data["suggestions"]]
        self.assertEqual(ids, chat_suggestions.suggestions_for(self.fixture.owner))
        self.assertTrue(ids)

    def test_anonymous_is_refused(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(AI_ASSISTANT_ENABLED=False)
    def test_nothing_while_the_assistant_is_off(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)
