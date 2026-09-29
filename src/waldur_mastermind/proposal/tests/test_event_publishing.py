import json
from datetime import timedelta
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework import status, test

from waldur_core.logging import event_dispatch
from waldur_core.logging.tests import factories as logging_factories
from waldur_core.permissions.fixtures import (
    CallRole,
    CustomerRole,
    ProjectRole,
    ProposalRole,
)
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks, utils
from waldur_mastermind.proposal.enums import CallStates, ProposalStates
from waldur_mastermind.proposal.tests import factories

DELAY = "waldur_core.logging.tasks.publish_messages.delay"


def _payloads_by_topic(mock_delay):
    """Call/proposal payloads per queue. Factories also emit user lifecycle
    events to the global consumer; those are not under test here."""
    result = {}
    for call in mock_delay.call_args_list:
        for message in call.args[0]:
            payload = json.loads(message["payload"])
            if payload["object_type"] in ("call", "proposal"):
                result.setdefault(message["topic"], []).append(payload)
    return result


def _call_organizer_role():
    return Role.objects.get_system_role(
        "CUSTOMER.CALL_ORGANIZER",
        content_type=ContentType.objects.get_for_model(models.CallManagingOrganisation),
    )


class ProposalEventPublishingTest(test.APITestCase):
    def setUp(self):
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.other_proposal = factories.ProposalFactory(round=self.round)

        other_call = factories.CallFactory(state=CallStates.ACTIVE)
        self.foreign_proposal = factories.ProposalFactory(
            round=factories.RoundFactory(call=other_call)
        )

        call_manager = structure_factories.UserFactory()
        self.call.add_user(call_manager, CallRole.MANAGER)
        self.call_consumer = self._consumer(call_manager, self.call)

        proposal_member = structure_factories.UserFactory()
        self.proposal.add_user(proposal_member, ProposalRole.MEMBER)
        self.proposal_consumer = self._consumer(proposal_member, self.proposal)

        call_organizer = structure_factories.UserFactory()
        self.call.manager.add_user(call_organizer, _call_organizer_role())
        self.call_organizer_consumer = self._consumer(call_organizer, self.call.manager)

        # Customer-level roles carry no PROPOSAL.LIST, so the API hides the
        # organisation's proposals from them; they receive call events only.
        organiser_owner = structure_factories.UserFactory()
        self.call.manager.customer.add_user(organiser_owner, CustomerRole.OWNER)
        self.organiser_consumer = self._consumer(
            organiser_owner, self.call.manager.customer
        )
        organiser_reader = structure_factories.UserFactory()
        self.call.manager.customer.add_user(organiser_reader, CustomerRole.READER)
        self.organiser_reader_consumer = self._consumer(
            organiser_reader, self.call.manager.customer
        )

        # The applicant's project is not in the proposal's scope chain.
        applicant_manager = structure_factories.UserFactory()
        self.proposal.project.add_user(applicant_manager, ProjectRole.MANAGER)
        self.applicant_consumer = self._consumer(
            applicant_manager, self.proposal.project
        )

        other_call_manager = structure_factories.UserFactory()
        other_call.add_user(other_call_manager, CallRole.MANAGER)
        self.other_call_consumer = self._consumer(other_call_manager, other_call)

        self.global_consumer = self._consumer(
            structure_factories.UserFactory(is_staff=True)
        )

    def _consumer(self, user, *scopes):
        return logging_factories.EventConsumerFactory.with_scopes(
            *scopes,
            user=user,
            queue_created=True,
            rmq_username=user.uuid.hex,
        )

    def _topic(self, consumer):
        return consumer.queue_name

    def _change_state(self, instance, state):
        with self.captureOnCommitCallbacks(execute=True):
            instance.state = state
            instance.save()

    @mock.patch(DELAY)
    def test_proposal_state_change_reaches_call_proposal_and_call_organizer(
        self, mock_delay
    ):
        self._change_state(self.proposal, ProposalStates.SUBMITTED)

        payloads = _payloads_by_topic(mock_delay)
        for consumer in (
            self.call_consumer,
            self.proposal_consumer,
            self.call_organizer_consumer,
            self.global_consumer,
        ):
            self.assertEqual(len(payloads[self._topic(consumer)]), 1)

        payload = payloads[self._topic(self.call_consumer)][0]
        self.assertEqual(payload["object_type"], "proposal")
        self.assertEqual(payload["event_type"], "proposal_state_changed")
        self.assertEqual(payload["proposal_uuid"], self.proposal.uuid.hex)
        self.assertEqual(payload["call_uuid"], self.call.uuid.hex)
        self.assertEqual(payload["round_uuid"], self.round.uuid.hex)
        self.assertEqual(payload["customer_uuid"], self.call.manager.customer.uuid.hex)
        self.assertEqual(payload["state"], ProposalStates.SUBMITTED)
        self.assertEqual(payload["previous_state"], ProposalStates.DRAFT)

    @mock.patch(DELAY)
    def test_proposal_event_does_not_reach_unrelated_consumers(self, mock_delay):
        self._change_state(self.proposal, ProposalStates.SUBMITTED)

        payloads = _payloads_by_topic(mock_delay)
        self.assertNotIn(self._topic(self.applicant_consumer), payloads)
        self.assertNotIn(self._topic(self.other_call_consumer), payloads)

    @mock.patch(DELAY)
    def test_proposal_event_does_not_reach_organiser_customer_roles(self, mock_delay):
        """The API gates customer-level access to proposals on PROPOSAL.LIST,
        which no customer role carries, so neither may pub/sub."""
        self._change_state(self.proposal, ProposalStates.SUBMITTED)

        payloads = _payloads_by_topic(mock_delay)
        self.assertNotIn(self._topic(self.organiser_consumer), payloads)
        self.assertNotIn(self._topic(self.organiser_reader_consumer), payloads)

    @mock.patch(DELAY)
    def test_proposal_bound_consumer_ignores_sibling_proposals(self, mock_delay):
        self._change_state(self.other_proposal, ProposalStates.SUBMITTED)

        payloads = _payloads_by_topic(mock_delay)
        self.assertNotIn(self._topic(self.proposal_consumer), payloads)
        self.assertIn(self._topic(self.call_consumer), payloads)

    @mock.patch(DELAY)
    def test_call_bound_consumer_ignores_other_calls_proposals(self, mock_delay):
        self._change_state(self.foreign_proposal, ProposalStates.SUBMITTED)

        payloads = _payloads_by_topic(mock_delay)
        self.assertNotIn(self._topic(self.call_consumer), payloads)
        self.assertIn(self._topic(self.other_call_consumer), payloads)

    @mock.patch(DELAY)
    def test_revoked_role_stops_delivery(self, mock_delay):
        self.call.remove_user(self.call_consumer.user)

        self._change_state(self.proposal, ProposalStates.SUBMITTED)

        self.assertNotIn(
            self._topic(self.call_consumer), _payloads_by_topic(mock_delay)
        )

    @mock.patch(DELAY)
    def test_save_without_state_change_publishes_nothing(self, mock_delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.proposal.project_summary = "Updated summary"
            self.proposal.save()

        self.assertEqual(_payloads_by_topic(mock_delay), {})

    @mock.patch(DELAY)
    def test_creating_a_draft_publishes_nothing(self, mock_delay):
        with self.captureOnCommitCallbacks(execute=True):
            factories.ProposalFactory(round=self.round)

        self.assertEqual(_payloads_by_topic(mock_delay), {})

    @mock.patch(DELAY)
    def test_closing_round_publishes_one_cancel_per_draft(self, mock_delay):
        self.other_proposal.state = ProposalStates.SUBMITTED
        self.other_proposal.save()
        second_draft = factories.ProposalFactory(round=self.round)
        mock_delay.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            utils.process_closed_round(self.round)

        # The whole round travels in a single task, not one per draft.
        self.assertEqual(mock_delay.call_count, 1)

        payloads = _payloads_by_topic(mock_delay)[self._topic(self.call_consumer)]
        self.assertEqual(
            {(p["proposal_uuid"], p["state"], p["previous_state"]) for p in payloads},
            {
                (proposal.uuid.hex, ProposalStates.CANCELED, ProposalStates.DRAFT)
                for proposal in (self.proposal, second_draft)
            },
        )
        for proposal in (self.proposal, second_draft):
            proposal.refresh_from_db()
            self.assertEqual(proposal.state, ProposalStates.CANCELED)

    @mock.patch(DELAY)
    def test_call_state_change_reaches_call_side_only(self, mock_delay):
        self._change_state(self.call, CallStates.ARCHIVED)

        payloads = _payloads_by_topic(mock_delay)
        for consumer in (
            self.call_consumer,
            self.call_organizer_consumer,
            self.organiser_consumer,
            self.organiser_reader_consumer,
            self.global_consumer,
        ):
            self.assertEqual(len(payloads[self._topic(consumer)]), 1)
        for consumer in (
            self.proposal_consumer,
            self.applicant_consumer,
            self.other_call_consumer,
        ):
            self.assertNotIn(self._topic(consumer), payloads)

        payload = payloads[self._topic(self.call_consumer)][0]
        self.assertEqual(payload["object_type"], "call")
        self.assertEqual(payload["event_type"], "call_state_changed")
        self.assertEqual(payload["call_uuid"], self.call.uuid.hex)
        self.assertEqual(payload["state"], CallStates.ARCHIVED)
        self.assertEqual(payload["previous_state"], CallStates.ACTIVE)

    @mock.patch(DELAY)
    def test_round_cutoff_publishes_cancel_for_drafts_only(self, mock_delay):
        self.other_proposal.state = ProposalStates.IN_REVIEW
        self.other_proposal.save()
        factories.ProposalFactory(round=self.round, state=ProposalStates.ACCEPTED)
        self.round.cutoff_time = timezone.now() - timedelta(minutes=1)
        self.round.save()
        mock_delay.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            tasks.proposals_for_ended_rounds_should_be_cancelled()

        # Only the draft is cancelled. The in-review proposal was submitted
        # before the cutoff and stays with its reviewers, so nothing is
        # published for it (nor for the accepted one).
        payloads = _payloads_by_topic(mock_delay)[self._topic(self.call_consumer)]
        self.assertEqual(
            {(p["proposal_uuid"], p["state"], p["previous_state"]) for p in payloads},
            {
                (self.proposal.uuid.hex, ProposalStates.CANCELED, ProposalStates.DRAFT),
            },
        )
        self.other_proposal.refresh_from_db()
        self.assertEqual(self.other_proposal.state, ProposalStates.IN_REVIEW)

    @mock.patch(DELAY)
    def test_submit_to_workflow_call_publishes_draft_to_in_review(self, mock_delay):
        # Calls are seeded with an enabled workflow step, so submit skips
        # SUBMITTED and goes straight to IN_REVIEW.
        self.assertTrue(
            models.CallWorkflowStep.objects.filter(
                call=self.call, is_enabled=True
            ).exists()
        )
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        self.round.start_time = timezone.now() - timedelta(days=1)
        self.round.cutoff_time = timezone.now() + timedelta(days=1)
        self.round.save()
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.ProposalFactory.get_url(self.proposal, "submit")
            )
        self.assertEqual(response.status_code, 200, response.data)

        payloads = _payloads_by_topic(mock_delay)[self._topic(self.call_consumer)]
        self.assertEqual(
            [(p["state"], p["previous_state"]) for p in payloads],
            [(ProposalStates.IN_REVIEW, ProposalStates.DRAFT)],
        )


# Registration provisions a real queue, so the broker has to be stubbed or the
# test measures RabbitMQ availability rather than the binding logic. Same set of
# patches as waldur_core.logging.tests.test_event_consumer_registration.
@mock.patch("waldur_core.logging.backend.RabbitMQManagementBackend.create_queue")
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.assign_rabbitmq_vhost_permissions"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_user"
)
@mock.patch(
    "waldur_core.logging.backend.RabbitMQManagementBackend.create_rabbitmq_virtual_host"
)
class ProposalBindingRegistrationTest(test.APITestCase):
    """Who may bind a consumer to a call or a proposal.

    The answer must be the set the dispatcher delivers to, which
    ProposalEventPublishingTest pins from the delivery side. Both read the chain
    registered with event_dispatch.register_event_chain; the generic
    get_scope_ancestors walk would resolve a proposal to the APPLICANT's project
    and customer, refusing the call manager below and accepting the applicant's
    project manager, whose queue would then receive nothing.
    """

    def setUp(self):
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)

        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)

        self.proposal_member = structure_factories.UserFactory()
        self.proposal.add_user(self.proposal_member, ProposalRole.MEMBER)

        self.organiser_owner = structure_factories.UserFactory()
        self.call.manager.customer.add_user(self.organiser_owner, CustomerRole.OWNER)

        self.call_organizer = structure_factories.UserFactory()
        self.call.manager.add_user(self.call_organizer, _call_organizer_role())

        self.applicant_manager = structure_factories.UserFactory()
        self.proposal.project.add_user(self.applicant_manager, ProjectRole.MANAGER)

    def _register(self, user, scope_type, uuid_value):
        self.client.force_authenticate(user)
        return self.client.post(
            "/api/event-consumers/register/",
            {"scopes": [{"type": scope_type, "uuid": uuid_value}], "object_types": []},
            format="json",
        )

    def test_call_manager_may_bind_to_one_proposal_of_their_call(self, *mocks):
        response = self._register(self.call_manager, "proposal", self.proposal.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_proposal_member_may_bind_to_their_proposal(self, *mocks):
        response = self._register(
            self.proposal_member, "proposal", self.proposal.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_call_organizer_may_bind_to_a_proposal(self, *mocks):
        response = self._register(
            self.call_organizer, "proposal", self.proposal.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_organiser_owner_may_not_bind_to_a_proposal(self, *mocks):
        """The organiser's customer is not in the proposal chain: its roles
        lack PROPOSAL.LIST, so such a binding would leak what the API hides."""
        response = self._register(
            self.organiser_owner, "proposal", self.proposal.uuid.hex
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_applicant_project_manager_may_not_bind_to_the_proposal(self, *mocks):
        """The applicant side is not in the event chain, so such a queue could
        never receive anything -- and delivery_blocked_reason would report it
        healthy, since it checks the wider registration chain."""
        response = self._register(
            self.applicant_manager, "proposal", self.proposal.uuid.hex
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_stranger_may_not_bind_to_the_proposal(self, *mocks):
        response = self._register(
            structure_factories.UserFactory(), "proposal", self.proposal.uuid.hex
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_call_manager_may_bind_to_their_call(self, *mocks):
        response = self._register(self.call_manager, "call", self.call.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_organiser_owner_may_bind_to_a_single_call(self, *mocks):
        """Documented in agent-pubsub.md: the customer is in the call's chain,
        so a role on it admits a binding to one call, not just to the customer."""
        response = self._register(self.organiser_owner, "call", self.call.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_organiser_owner_may_bind_to_the_call_organizer(self, *mocks):
        response = self._register(
            self.organiser_owner, "call_organizer", self.call.manager.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_stranger_may_not_bind_to_the_call(self, *mocks):
        response = self._register(
            structure_factories.UserFactory(), "call", self.call.uuid.hex
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )


class DeliveryBlockedReasonTest(test.APITestCase):
    """The diagnostic resolves call and proposal bindings through the registered
    chain, so it agrees with what registration admits and delivery sends."""

    def setUp(self):
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        self.proposal = factories.ProposalFactory(
            round=factories.RoundFactory(call=self.call)
        )

    def _consumer(self, user, scope):
        return logging_factories.EventConsumerFactory.with_scopes(
            scope, user=user, queue_created=True, rmq_username=user.uuid.hex
        )

    def test_call_manager_bound_to_a_proposal_is_healthy(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.MANAGER)

        consumer = self._consumer(user, self.proposal)

        self.assertIsNone(event_dispatch.delivery_blocked_reason(consumer))

    def test_call_organizer_bound_to_a_call_is_healthy(self):
        user = structure_factories.UserFactory()
        self.call.manager.add_user(user, _call_organizer_role())

        consumer = self._consumer(user, self.call)

        self.assertIsNone(event_dispatch.delivery_blocked_reason(consumer))

    def test_applicant_project_manager_bound_to_a_proposal_is_blocked(self):
        """A binding stored before the guard existed receives nothing."""
        user = structure_factories.UserFactory()
        self.proposal.project.add_user(user, ProjectRole.MANAGER)

        consumer = self._consumer(user, self.proposal)

        self.assertIsNotNone(event_dispatch.delivery_blocked_reason(consumer))

    def test_organiser_owner_bound_to_a_proposal_is_blocked(self):
        user = structure_factories.UserFactory()
        self.call.manager.customer.add_user(user, CustomerRole.OWNER)

        consumer = self._consumer(user, self.proposal)

        self.assertIsNotNone(event_dispatch.delivery_blocked_reason(consumer))
