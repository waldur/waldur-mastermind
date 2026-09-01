import pytest
from django.test import TestCase
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.support import models
from waldur_mastermind.support.tests import factories

# `base.BaseTest` mocks `get_active_backend` wholesale and forces every
# `*_is_available` to True, which is exactly the gate under test here, so these
# cases drive the real backends instead.
BASIC = pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)


# Plain APITestCase: nothing here depends on committed data, so there is no
# reason to pay for (or to add to the repo's budget of) a transaction test case.
class BaseSetStatusTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        models.IssueStatus.objects.create(
            name="Resolved", type=models.IssueStatus.Types.RESOLVED
        )
        models.IssueStatus.objects.create(
            name="Canceled", type=models.IssueStatus.Types.CANCELED
        )
        self.issue = factories.IssueFactory(
            status="Open",
            backend_name="basic",
            customer=self.fixture.customer,
            project=self.fixture.project,
        )
        self.url = factories.IssueFactory.get_url(self.issue, action="set_status")

    def set_status(self, user, value):
        self.client.force_authenticate(user)
        return self.client.post(self.url, {"status": value})


@BASIC
class SetStatusTest(BaseSetStatusTest):
    def test_staff_can_move_issue_to_available_status(self):
        response = self.set_status(self.fixture.staff, "Resolved")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Resolved")

    def test_support_can_move_issue_to_available_status(self):
        response = self.set_status(self.fixture.global_support, "Resolved")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_regular_user_cannot_change_status(self):
        response = self.set_status(self.fixture.user, "Resolved")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_cannot_change_status(self):
        response = self.set_status(self.fixture.owner, "Resolved")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_resolving_stamps_resolution_date(self):
        self.assertIsNone(self.issue.resolution_date)
        self.set_status(self.fixture.staff, "Resolved")
        self.issue.refresh_from_db()
        self.assertIsNotNone(self.issue.resolution_date)

    def test_resolved_issue_reports_sla_as_met(self):
        self.set_status(self.fixture.staff, "Resolved")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.IssueFactory.get_url(self.issue))
        self.assertEqual(response.data["sla_status"], "met")

    def test_unknown_status_is_rejected(self):
        response = self.set_status(self.fixture.staff, "Nonexistent")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Open")

    def test_transition_outside_the_configured_workflow_is_rejected(self):
        factories.IssueStatusTransitionFactory(from_status="Open", to_status="Canceled")
        # A 400, not the 500 that an uncaught SupportBackendError would produce.
        response = self.set_status(self.fixture.staff, "Resolved")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Open")

    def test_available_statuses_follow_the_configured_workflow(self):
        factories.IssueStatusTransitionFactory(
            from_status="Open", to_status="In progress"
        )
        factories.IssueStatusTransitionFactory(
            from_status="In progress", to_status="Resolved"
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.IssueFactory.get_url(self.issue))
        self.assertEqual(response.data["available_statuses"], ["In progress"])

    def test_available_statuses_fall_back_to_registered_statuses(self):
        # No workflow configured: every registered status is on offer, plus the
        # default one, so a ticket resolved by mistake can be reopened.
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.IssueFactory.get_url(self.issue))
        self.assertEqual(response.data["available_statuses"], ["Canceled", "Resolved"])

    def test_a_resolved_issue_can_be_reopened(self):
        self.set_status(self.fixture.staff, "Resolved")
        self.issue.refresh_from_db()
        self.assertIsNotNone(self.issue.resolution_date)

        response = self.set_status(self.fixture.staff, "Open")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Open")
        # Reopening has to clear the resolution date, or the issue stays out of
        # Issue.objects.open() and keeps reporting a met SLA for good.
        self.assertIsNone(self.issue.resolution_date)
        self.assertEqual(models.Issue.objects.open().count(), 1)

    def test_moving_between_terminal_statuses_keeps_the_closure_time(self):
        self.set_status(self.fixture.staff, "Resolved")
        self.issue.refresh_from_db()
        closed_at = self.issue.resolution_date

        self.set_status(self.fixture.staff, "Canceled")
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.resolution_date, closed_at)

    def test_routed_issue_status_belongs_to_the_provider(self):
        helpdesk = factories.ProviderHelpdeskFactory()
        self.issue.provider_helpdesk = helpdesk
        self.issue.save()

        response = self.set_status(self.fixture.staff, "Resolved")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Open")


@BASIC
class BulkSetStatusTest(BaseSetStatusTest):
    def setUp(self):
        super().setUp()
        self.other = factories.IssueFactory(
            status="Waiting", backend_name="basic", customer=self.fixture.customer
        )
        self.bulk_url = factories.IssueFactory.get_list_url() + "bulk_update/"

    def bulk_set(self, value):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(
            self.bulk_url,
            {
                "issue_uuids": [self.issue.uuid.hex, self.other.uuid.hex],
                "status": value,
            },
        )

    def test_bulk_update_applies_to_every_issue(self):
        response = self.bulk_set("Resolved")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.issue.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.issue.status, "Resolved")
        self.assertEqual(self.other.status, "Resolved")

    def test_a_rejected_batch_leaves_every_issue_untouched(self):
        # Only the first issue may move; the batch must not half-apply. DRF
        # renders the ValidationError into a 400 inside the request's atomic
        # block, and its set_rollback() is a no-op without ATOMIC_REQUESTS, so
        # anything written before the raise would have been committed.
        factories.IssueStatusTransitionFactory(from_status="Open", to_status="Resolved")

        response = self.bulk_set("Resolved")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.issue.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.issue.status, "Open")
        self.assertEqual(self.other.status, "Waiting")

    def test_bulk_update_rejects_an_unknown_status(self):
        response = self.bulk_set("Nonexistent")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Open")


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="atlassian",
)
class ExternalBackendIsUntouchedTest(BaseSetStatusTest):
    """The remote service desk owns the status of an externally-backed issue.

    Jira, Zammad and SMAX all inherit `update_is_available` as False, so the
    action must refuse before it ever reaches the handler.
    """

    def test_staff_cannot_change_status(self):
        response = self.set_status(self.fixture.staff, "Resolved")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Open")

    def test_no_statuses_are_offered(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.IssueFactory.get_url(self.issue))
        self.assertEqual(response.data["available_statuses"], [])

    def test_bulk_update_cannot_change_status(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.IssueFactory.get_list_url() + "bulk_update/",
            {"issue_uuids": [self.issue.uuid.hex], "status": "Resolved"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Open")


@BASIC
class SupportStatisticsTest(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        models.IssueStatus.objects.create(
            name="Resolved", type=models.IssueStatus.Types.RESOLVED
        )
        models.IssueStatus.objects.create(
            name="Canceled", type=models.IssueStatus.Types.CANCELED
        )
        self.client = test.APIClient()
        self.client.force_authenticate(self.fixture.staff)

    def get_stats(self):
        return self.client.get("/api/support-statistics/").json()

    def test_open_issue_is_counted_as_open(self):
        factories.IssueFactory(status="Open", backend_name="basic")
        self.assertEqual(self.get_stats()["open_issues_count"], 1)

    def test_resolved_issue_leaves_the_open_count(self):
        # The old query compared the status name against the integer members of
        # IssueStatus.Types, so a resolved ticket stayed "open" forever.
        issue = factories.IssueFactory(status="Open", backend_name="basic")
        issue.set_resolved()
        stats = self.get_stats()
        self.assertEqual(stats["open_issues_count"], 0)
        self.assertEqual(stats["closed_this_month_count"], 1)


@pytest.mark.override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=True,
)
class SupportStatisticsAccessTest(test.APITestCase):
    """Who may read the deployment-wide ticket counts.

    Until this gate the endpoint had no permission class at all, so any
    authenticated user could read them.
    """

    url = "/api/support-statistics/"

    def setUp(self):
        self.fixture = structure_fixtures.ServiceFixture()
        models.IssueStatus.objects.create(
            name="Resolved", type=models.IssueStatus.Types.RESOLVED
        )
        self.helpdesk = factories.ProviderHelpdeskFactory()
        # One ticket for this provider, one for nobody in particular.
        self.provider_issue = factories.IssueFactory(
            status="Open", backend_name="basic", provider_helpdesk=self.helpdesk
        )
        self.other_issue = factories.IssueFactory(status="Open", backend_name="basic")

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url)

    def test_staff_sees_the_whole_deployment(self):
        response = self.get(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["open_issues_count"], 2)

    def test_support_sees_the_whole_deployment(self):
        response = self.get(self.fixture.global_support)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["open_issues_count"], 2)

    def test_regular_user_is_refused(self):
        self.assertEqual(
            self.get(self.fixture.user).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_customer_owner_without_a_helpdesk_is_refused(self):
        self.assertEqual(
            self.get(self.fixture.owner).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_provider_agent_sees_only_their_own_tickets(self):
        agent = structure_factories.UserFactory()
        models.ProviderSupportUser.objects.create(
            user=agent, provider_helpdesk=self.helpdesk, is_active=True
        )
        response = self.get(agent)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["open_issues_count"], 1)
        # Broadcasts are the operator talking to their users.
        self.assertEqual(body["recent_broadcasts_count"], 0)

    def test_inactive_provider_agent_is_refused(self):
        agent = structure_factories.UserFactory()
        models.ProviderSupportUser.objects.create(
            user=agent, provider_helpdesk=self.helpdesk, is_active=False
        )
        self.assertEqual(self.get(agent).status_code, status.HTTP_403_FORBIDDEN)

    @pytest.mark.override_config(WALDUR_SUPPORT_PROVIDER_ROUTING_ENABLED=False)
    def test_provider_agent_is_refused_when_routing_is_off(self):
        agent = structure_factories.UserFactory()
        models.ProviderSupportUser.objects.create(
            user=agent, provider_helpdesk=self.helpdesk, is_active=True
        )
        self.assertEqual(self.get(agent).status_code, status.HTTP_403_FORBIDDEN)
