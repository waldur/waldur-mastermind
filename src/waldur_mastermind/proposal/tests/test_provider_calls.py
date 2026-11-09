"""The provider side of a call: where the calls its offerings are in stand,
which steps the offering's team acts in, whether anyone is on that team, the
provider's own call work, and the mail telling it a call wants an offering."""

from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.models import Notification
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models, my_tasks, provider_calls
from waldur_mastermind.proposal import workflow_checks as wc
from waldur_mastermind.proposal.enums import (
    CallStates,
    ContributorSources,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    RequestedOfferingStates,
    ResponsibleRoles,
    WorkflowPhases,
)
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_my_tasks import MyTasksBase

MY_TASKS_URL = "http://testserver/api/proposal-my-tasks/"


def enable_technical_assessment(call):
    """The call's technical assessment, done by each requested offering's
    team and completed by the offering managers."""
    step = call.workflow_steps.get(key="technical_assessment")
    step.is_enabled = True
    step.completer = ResponsibleRoles.OFFERING_MANAGER
    step.contributor_source = ContributorSources.REQUESTED_OFFERING_TEAM
    step.per_requested_offering = True
    step.save()
    return step


def disable_team_steps(call):
    call.workflow_steps.filter(provider_calls.team_steps_q()).update(is_enabled=False)


class ProviderCallsBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.requested = self.fixture.requested_offering_accepted
        self.offering = self.requested.offering
        self.provider_owner = self.fixture.offering_owner
        self.step = enable_technical_assessment(self.call)

    def rows(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.RequestedOfferingFactory.get_provider_list_url(),
            {"call_uuid": self.call.uuid.hex, **params},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.json()

    def row(self):
        (row,) = self.rows(self.provider_owner, state=RequestedOfferingStates.ACCEPTED)
        return row


class RequestedOfferingCallStatusTest(ProviderCallsBase):
    def test_row_says_where_the_call_stands(self):
        row = self.row()
        self.assertEqual(row["call_uuid"], self.call.uuid.hex)
        self.assertEqual(row["call_state"], self.call.state)
        self.assertEqual(row["current_round"]["uuid"], self.fixture.round.uuid.hex)
        self.assertEqual(row["current_round"]["status"], "open")

    def test_call_without_rounds_has_no_current_round(self):
        call = self.fixture.new_call
        factories.RequestedOfferingFactory(call=call, offering=self.offering)
        (row,) = self.rows(self.provider_owner, call_uuid=call.uuid.hex)
        self.assertIsNone(row["current_round"])

    def test_drafts_are_not_counted_as_proposals(self):
        self.assertEqual(self.fixture.proposal.state, ProposalStates.DRAFT)
        self.assertEqual(self.row()["proposals_count"], 0)
        self.fixture.proposal.state = ProposalStates.SUBMITTED
        self.fixture.proposal.save()
        self.assertEqual(self.row()["proposals_count"], 1)

    def test_counts_the_offering_managers(self):
        self.assertEqual(self.row()["offering_managers_count"], 0)
        self.offering.add_user(structure_factories.UserFactory(), OfferingRole.MANAGER)
        self.assertEqual(self.row()["offering_managers_count"], 1)

    def test_counts_the_steps_the_team_acts_in(self):
        self.assertEqual(self.row()["team_steps_count"], 1)
        disable_team_steps(self.call)
        self.assertEqual(self.row()["team_steps_count"], 0)

    def test_filters_by_call_state(self):
        self.assertTrue(self.rows(self.provider_owner, call_state="active"))
        self.assertEqual(self.rows(self.provider_owner, call_state="archived"), [])


class CurrentRoundTest(test.APITestCase):
    def setUp(self):
        self.call = factories.CallFactory()
        self.now = timezone.now()

    def round(self, start_days, cutoff_days):
        return factories.RoundFactory(
            call=self.call,
            start_time=self.now + timedelta(days=start_days),
            cutoff_time=self.now + timedelta(days=cutoff_days),
        )

    def current(self):
        return provider_calls.current_round(self.call.round_set.all())

    def test_open_round_wins(self):
        self.round(-30, -20)
        open_round = self.round(-1, 10)
        self.round(20, 30)
        self.assertEqual(self.current(), open_round)

    def test_next_round_when_none_is_open(self):
        self.round(-30, -20)
        upcoming = self.round(20, 30)
        self.round(40, 50)
        self.assertEqual(self.current(), upcoming)

    def test_last_ended_round_when_all_ended(self):
        self.round(-60, -50)
        last = self.round(-30, -20)
        self.assertEqual(self.current(), last)


class TeamStepsTest(ProviderCallsBase):
    def url(self):
        return factories.RequestedOfferingFactory.get_provider_url(
            self.requested, "team_steps"
        )

    def get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(self.url())

    def test_lists_only_the_steps_the_team_acts_in(self):
        response = self.get(self.provider_owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        (row,) = response.json()
        self.assertEqual(row["uuid"], self.step.uuid.hex)
        self.assertEqual(row["name"], self.step.name)
        self.assertEqual(row["team_part"], provider_calls.TeamParts.COMPLETES)
        self.assertTrue(row["per_requested_offering"])
        enabled = self.call.workflow_steps.filter(is_enabled=True).count()
        self.assertGreater(enabled, 1)

    def test_contributing_team_is_told_apart_from_completing_one(self):
        self.step.completer = ResponsibleRoles.CALL_MANAGER
        self.step.save()
        (row,) = self.get(self.provider_owner).json()
        self.assertEqual(row["team_part"], provider_calls.TeamParts.CONTRIBUTES)

    def test_names_the_events_the_team_is_mailed_about(self):
        self.step.notification_rules.all().delete()
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.step,
            trigger=NotificationRuleTriggers.STEP_STARTED,
            recipient=NotificationRuleRecipients.RESPONSIBLE_ROLE,
        )
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.step,
            trigger=NotificationRuleTriggers.STEP_EXPIRED,
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
        )
        (row,) = self.get(self.provider_owner).json()
        self.assertEqual(
            row["notification_triggers"], [NotificationRuleTriggers.STEP_STARTED]
        )

    def test_offering_manager_reads_them(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.ACCEPT_REQUESTED_OFFERING)
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        self.assertEqual(self.get(manager).status_code, status.HTTP_200_OK)

    def test_outsiders_do_not(self):
        for user in (
            self.fixture.owner,
            self.fixture.call_manager,
            structure_factories.UserFactory(),
        ):
            self.assertEqual(
                self.get(user).status_code, status.HTTP_404_NOT_FOUND, user
            )


class OfferingWithoutManagersCheckTest(ProviderCallsBase):
    def findings(self):
        return [
            f
            for f in wc.check_call(self.call)
            if f["code"] == "offering_without_managers"
        ]

    def test_warns_naming_the_offering_steps_and_provider(self):
        self.offering.customer.email = "allocations@provider.example.org"
        self.offering.customer.save()
        (finding,) = self.findings()
        self.assertEqual(finding["severity"], wc.WARNING)
        self.assertEqual(finding["field"], wc.CALL_OFFERINGS)
        self.assertIn(self.offering.name, finding["message"])
        self.assertIn(self.step.name, finding["message"])
        self.assertIn(self.offering.customer.name, finding["message"])
        self.assertIn("allocations@provider.example.org", finding["message"])

    def test_quiet_once_the_offering_has_a_manager(self):
        self.offering.add_user(structure_factories.UserFactory(), OfferingRole.MANAGER)
        self.assertEqual(self.findings(), [])

    def test_quiet_when_no_step_needs_the_team(self):
        disable_team_steps(self.call)
        self.assertEqual(self.findings(), [])

    def test_canceled_request_does_not_count(self):
        models.RequestedOffering.objects.filter(offering=self.offering).update(
            state=RequestedOfferingStates.CANCELED
        )
        self.assertEqual(self.findings(), [])

    def test_does_not_block_activation(self):
        self.assertTrue(self.findings())
        self.assertNotIn(
            "offering_without_managers",
            {f["code"] for f in wc.errors(wc.check_call(self.call))},
        )


class OfferingRequestTaskTest(ProviderCallsBase):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.ACCEPT_REQUESTED_OFFERING)
        self.pending = self.fixture.requested_offering

    def tasks(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(MY_TASKS_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [
            row
            for row in response.json()
            if row["needed"] == my_tasks.Needs.ANSWER_OFFERING_REQUEST
        ]

    def test_provider_owner_answers_the_request(self):
        (row,) = self.tasks(self.provider_owner)
        self.assertEqual(row["key"], f"offering-request:{self.pending.uuid.hex}")
        self.assertEqual(row["target"], my_tasks.Targets.OFFERING_REQUESTS)
        self.assertEqual(row["target_uuid"], self.offering.customer.uuid.hex)
        self.assertEqual(row["call_uuid"], self.call.uuid.hex)
        self.assertEqual(row["offering_uuid"], self.offering.uuid.hex)
        self.assertEqual(row["offering_name"], self.offering.name)
        self.assertEqual(row["provider_uuid"], self.offering.customer.uuid.hex)
        self.assertEqual(row["provider_name"], self.offering.customer.name)

    def test_offering_manager_answers_it_too(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.ACCEPT_REQUESTED_OFFERING)
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        self.assertEqual(len(self.tasks(manager)), 1)

    def test_answered_request_leaves_the_list(self):
        self.pending.state = RequestedOfferingStates.ACCEPTED
        self.pending.save()
        self.assertEqual(self.tasks(self.provider_owner), [])

    def test_archived_call_asks_nothing(self):
        self.call.state = CallStates.ARCHIVED
        self.call.save(update_fields=["state"])
        self.assertEqual(self.tasks(self.provider_owner), [])

    def test_others_have_nothing_to_answer(self):
        for user in (
            self.fixture.owner,
            self.fixture.call_manager,
            self.fixture.staff,
            structure_factories.UserFactory(),
        ):
            self.assertEqual(self.tasks(user), [], user)

    def test_filtered_by_provider_and_offering(self):
        provider = self.offering.customer.uuid.hex
        self.assertEqual(
            len(self.tasks(self.provider_owner, provider_uuid=provider)), 1
        )
        self.assertEqual(
            self.tasks(
                self.provider_owner,
                provider_uuid=structure_factories.CustomerFactory().uuid.hex,
            ),
            [],
        )
        other = marketplace_factories.OfferingFactory()
        self.assertEqual(
            self.tasks(self.provider_owner, offering_uuid=other.uuid.hex), []
        )
        self.assertEqual(
            len(self.tasks(self.provider_owner, offering_uuid=str(self.offering.uuid))),
            1,
        )


class AssessmentTaskOfferingTest(MyTasksBase):
    def setUp(self):
        super().setUp()
        self.move_to("technical_assessment")

    def test_slot_names_its_offering_and_provider(self):
        (row,) = self.tasks(self.manager_a)
        offering = self.offering_a.offering
        self.assertEqual(row["offering_uuid"], offering.uuid.hex)
        self.assertEqual(row["offering_name"], offering.name)
        self.assertEqual(row["provider_uuid"], offering.customer.uuid.hex)
        self.assertEqual(row["phase"], WorkflowPhases.EVALUATION)

    def test_provider_filter_keeps_its_own_slot(self):
        self.client.force_authenticate(self.manager_a)
        own = self.offering_a.offering.customer.uuid.hex
        other = self.offering_b.offering.customer.uuid.hex
        response = self.client.get(MY_TASKS_URL, {"provider_uuid": own})
        self.assertEqual(len(response.json()), 1)
        response = self.client.get(MY_TASKS_URL, {"provider_uuid": other})
        self.assertEqual(response.json(), [])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingRequestMailTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.offering = marketplace_factories.OfferingFactory()
        self.owner = structure_factories.UserFactory(email="owner@provider.example.org")
        self.offering.customer.add_user(self.owner, CustomerRole.OWNER)
        structure_factories.NotificationFactory(
            key="proposal.requested_offering_created"
        )

    def request(self, state=RequestedOfferingStates.REQUESTED):
        with self.captureOnCommitCallbacks(execute=True):
            return factories.RequestedOfferingFactory(
                call=self.call,
                offering=self.offering,
                state=state,
                created_by=self.fixture.owner,
                description="Please join with your GPU partition.",
            )

    def recipients(self):
        return sorted(address for message in mail.outbox for address in message.to)

    def test_tells_managers_and_owners(self):
        manager = structure_factories.UserFactory(email="manager@provider.example.org")
        self.offering.add_user(manager, OfferingRole.MANAGER)
        self.request()
        self.assertEqual(
            self.recipients(),
            ["manager@provider.example.org", "owner@provider.example.org"],
        )
        message = mail.outbox[0]
        self.assertIn(self.call.name, message.subject)
        self.assertIn(self.offering.name, message.body)
        self.assertIn("Please join with your GPU partition.", message.body)
        self.assertIn(
            f"providers/{self.offering.customer.uuid.hex}/offering-requests/",
            message.body,
        )
        self.assertNotIn("no offering manager yet", message.body)

    def test_asks_for_offering_managers_when_the_team_is_needed(self):
        enable_technical_assessment(self.call)
        self.request()
        (message,) = mail.outbox
        self.assertEqual(message.to, ["owner@provider.example.org"])
        self.assertIn("Technical assessment", message.body)
        self.assertIn("no offering manager yet", message.body)
        self.assertIn("?tab=permissions", message.body)

    def test_no_ask_when_no_step_needs_the_team(self):
        disable_team_steps(self.call)
        self.request()
        (message,) = mail.outbox
        self.assertNotIn("no offering manager yet", message.body)

    def test_skips_people_who_opted_out(self):
        self.owner.notifications_enabled = False
        self.owner.save()
        self.request()
        self.assertEqual(mail.outbox, [])

    def test_only_new_requests(self):
        self.request(state=RequestedOfferingStates.ACCEPTED)
        self.assertEqual(mail.outbox, [])
        requested = self.request()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            requested.description = "Changed"
            requested.save()
        self.assertEqual(mail.outbox, [])

    def test_disabled_notification_sends_nothing(self):
        Notification.objects.filter(key="proposal.requested_offering_created").update(
            enabled=False
        )
        self.request()
        self.assertEqual(mail.outbox, [])


class ProviderRequestedResourcesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.url = factories.RequestedResourceFactory.get_provider_list_url()

    def uuids(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["uuid"] for row in response.json()]

    def submit(self):
        self.fixture.proposal.state = ProposalStates.SUBMITTED
        self.fixture.proposal.save()

    def test_provider_does_not_see_draft_proposals(self):
        self.assertEqual(self.uuids(self.fixture.offering_owner), [])
        self.submit()
        self.assertEqual(
            self.uuids(self.fixture.offering_owner),
            [self.requested_resource.uuid.hex],
        )

    def test_row_names_the_proposal_call_and_offering(self):
        self.submit()
        self.client.force_authenticate(self.fixture.offering_owner)
        (row,) = self.client.get(self.url).json()
        self.assertEqual(row["proposal_uuid"], self.fixture.proposal.uuid.hex)
        self.assertEqual(row["proposal_state"], ProposalStates.SUBMITTED)
        self.assertEqual(row["call_uuid"], self.fixture.call.uuid.hex)
        self.assertEqual(row["call_name"], self.fixture.call.name)
        self.assertEqual(row["round_name"], self.fixture.proposal.round.name)
        self.assertEqual(row["offering_uuid"], self.fixture.offering.uuid.hex)
        self.assertEqual(row["offering_name"], self.fixture.offering.name)

    def test_filters_by_provider_and_call(self):
        self.submit()
        owner = self.fixture.offering_owner
        provider = self.fixture.offering.customer.uuid.hex
        self.assertEqual(len(self.uuids(owner, provider_uuid=provider)), 1)
        self.assertEqual(
            self.uuids(
                owner, provider_uuid=structure_factories.CustomerFactory().uuid.hex
            ),
            [],
        )
        self.assertEqual(
            len(self.uuids(owner, call_uuid=self.fixture.call.uuid.hex)), 1
        )
        self.assertEqual(
            self.uuids(owner, call_uuid=self.fixture.new_call.uuid.hex), []
        )
