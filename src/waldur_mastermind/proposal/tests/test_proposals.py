import datetime
from unittest import mock

from ddt import data, ddt
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.permissions.utils import has_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks, utils
from waldur_mastermind.proposal.enums import AllocationTimes, CallStates, ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


@ddt
class ProposalGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_url(self.fixture.proposal)

    @data(
        "staff", "call_manager", "proposal_creator", "reviewer_1", "call_organizer_user"
    )
    def test_proposal_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_proposal_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_proposal_should_not_be_visible_if_user_is_not_connected_to_call(self):
        another_reviewer, another_proposals_url = (
            self.create_another_call_and_proposal()
        )

        user = another_reviewer
        self.client.force_authenticate(user)
        response = self.client.get(another_proposals_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def create_another_call_and_proposal(self):
        another_call = factories.CallFactory(
            manager=self.fixture.manager,
            state=CallStates.ACTIVE,
        )
        another_round = factories.RoundFactory(call=another_call)
        another_reviewer = structure_factories.UserFactory()
        another_call.add_user(another_reviewer, CallRole.REVIEWER)
        another_proposal = factories.ProposalFactory(round=another_round)
        another_proposals_url = factories.ProposalFactory.get_url(another_proposal)
        return another_reviewer, another_proposals_url


@ddt
class ProposalCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_list_url()

    @data(
        "staff",
        "owner",
        "customer_support",
        "user",
        "call_manager",
    )
    def test_user_can_add_proposal(self, user):
        response = self.create_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        proposal = models.Proposal.objects.get(uuid=response.data["uuid"])
        self.assertTrue(
            models.Proposal.objects.filter(uuid=response.data["uuid"]).exists()
        )
        # Check if user has been added to proposal in MANAGER role
        self.assertTrue(
            has_user(proposal, getattr(self.fixture, user), ProposalRole.MANAGER)
        )

    def test_project_has_not_been_created_if_proposal_has_been_created(self):
        response = self.create_proposal("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Proposal.objects.filter(uuid=response.data["uuid"]).exists()
        )
        proposal = models.Proposal.objects.get(uuid=response.data["uuid"])
        self.assertFalse(proposal.project)

    def create_proposal(self, user, **kwargs):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "name": "new",
            "round_uuid": self.fixture.round.uuid.hex,
            "duration_in_days": 10,
        }
        payload.update(kwargs)

        return self.client.post(self.url, payload)


@ddt
class UpdateProposalProjectDetailsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.url = factories.ProposalFactory.get_url(
            self.proposal, "update_project_details"
        )

    @data(
        "staff",
        "proposal_creator",
    )
    def test_user_can_update_proposal(self, user):
        response = self.update_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_update_proposal(self, user):
        response = self.update_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def _upload_proposal_document(self):
        url = factories.ProposalFactory.get_url(self.proposal, action="attach_document")
        payload = {"file": dummy_image()}
        return self.client.post(url, payload, format="multipart")

    @data("staff", "call_manager")
    def test_upload_documents(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self._upload_proposal_document()
        proposal = models.Proposal.objects.get(uuid=self.proposal.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(proposal.proposaldocumentation_set.count(), 1)

    def _detach_proposal_document(self, doc_uuids):
        url = factories.ProposalFactory.get_url(
            self.proposal, action="detach_documents"
        )
        payload = {"documents": doc_uuids}
        return self.client.post(url, payload)

    @data("staff", "call_manager")
    def test_detach_documents(self, user):
        # First upload a document
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        self._upload_proposal_document()
        proposal = models.Proposal.objects.get(uuid=self.proposal.uuid)
        self.assertEqual(proposal.proposaldocumentation_set.count(), 1)

        # Then detach it
        doc = proposal.proposaldocumentation_set.first()
        response = self._detach_proposal_document([str(doc.uuid)])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        proposal.refresh_from_db()
        self.assertEqual(proposal.proposaldocumentation_set.count(), 0)

    @data("staff", "call_manager")
    def test_detach_nonexistent_document(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        # Try to detach a non-existent document - should succeed without error
        response = self._detach_proposal_document(
            ["00000000-0000-0000-0000-000000000000"]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "staff",
        "proposal_creator",
    )
    def test_user_can_not_update_not_draft_proposal(self, user):
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        response = self.update_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def update_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "name": "new",
            "duration_in_days": 10,
        }
        response = self.client.post(self.url, payload)
        self.proposal.refresh_from_db()
        return response


@ddt
class ProposalDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.url = factories.ProposalFactory.get_url(self.proposal)

    @data(
        "staff",
        "proposal_creator",
    )
    def test_user_can_delete(self, user):
        response = self.delete_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data(
        "owner",
        "customer_support",
    )
    def test_customer_user_can_not_delete(self, user):
        response = self.delete_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("user")
    def test_user_can_not_delete(self, user):
        response = self.delete_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def delete_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)


@ddt
class ActionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        # A proposal must have a project team to be submitted (in production the
        # creator is auto-added; the factory doesn't, so add them here).
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        # Tests in this class exercise the no-workflow submission path
        # (DRAFT -> SUBMITTED). Clear the auto-seeded allocation_decision
        # step so submit() doesn't transition the proposal into IN_REVIEW.
        models.CallWorkflowStep.objects.filter(call=self.proposal.round.call).delete()
        self.submit_url = factories.ProposalFactory.get_url(self.proposal, "submit")
        structure_factories.NotificationFactory(
            key="proposal.proposal_state_changed",
        )
        structure_factories.NotificationFactory(
            key="proposal.new_proposal_submitted",
        )
        structure_factories.NotificationFactory(
            key="proposal.proposal_decision_for_reviewer",
        )

    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_user_about_proposal_state_update.delay"
    )
    @data(
        "staff",
        "proposal_creator",
    )
    def test_user_can_submit_proposal(self, user, mock_notify):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.submit_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertTrue(self.proposal.state, ProposalStates.SUBMITTED)

        # Verify that notification task has been called
        mock_notify.assert_called_once()

    @override_settings(task_always_eager=True)
    @data("proposal_creator")
    def test_notifications_are_sent_after_submission(self, user):
        user = getattr(self.fixture, user)
        call_manager = self.fixture.call_manager
        self.proposal.round.call.add_user(call_manager, CallRole.MANAGER)
        self.client.force_authenticate(user)
        self.client.post(self.submit_url)
        # Verify that notification email has been sent to proposal creator
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].to, [user.email])
        # Verify notification content
        proposal_url = core_utils.format_homeport_link(
            "proposals/{proposal_uuid}/",
            proposal_uuid=self.proposal.uuid,
        )
        body = mail.outbox[0].body
        self.assertIn("Your proposal has been successfully submitted", body)
        self.assertIn(f"Dear {user.full_name}", body)
        self.assertIn(self.proposal.name, body)
        self.assertIn(f"Previous state: {ProposalStates.DRAFT}", body)
        self.assertIn(f"New state: {ProposalStates.SUBMITTED}", body)
        self.assertIn(f"View Proposal: {proposal_url}", body)

        # Verify that notification email has been sent to call manager
        self.assertEqual(mail.outbox[1].to, [call_manager.email])
        proposal_url = core_utils.format_homeport_link(
            "call-management/{customer_uuid}/proposals/{proposal_uuid}/",
            customer_uuid=self.proposal.round.call.manager.customer.uuid,
            proposal_uuid=self.proposal.uuid,
        )
        body = mail.outbox[1].body
        self.assertIn("A new proposal has been submitted to the call", body)
        self.assertIn(self.proposal.name, body)
        self.assertIn(proposal_url, body)
        self.assertIn(self.proposal.created_by.full_name, body)
        self.assertIn(self.proposal.round.name, body)
        self.assertIn(self.proposal.round.call.name, body)

    @data(
        "owner",
        "customer_support",
    )
    def test_user_can_not_submit_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.submit_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_set_project_start_date_on_fixed_date_allocation(self):
        new_proposal = factories.ProposalFactory(
            round=self.fixture.round,
            state=ProposalStates.IN_REVIEW,
            project=None,
        )
        allocation_date = timezone.now() + datetime.timedelta(weeks=1)
        new_proposal.round.allocation_date = allocation_date
        new_proposal.round.save()
        # Allocation timing is a call-level policy on the allocation_decision
        # workflow step; the concrete date stays on the round.
        models.CallWorkflowStep.objects.update_or_create(
            call=new_proposal.round.call,
            step="allocation_decision",
            defaults={"allocation_time": AllocationTimes.FIXED_DATE},
        )

        utils.allocate_proposal(new_proposal, approved_by=self.fixture.staff)
        new_proposal.refresh_from_db()
        self.assertEqual(new_proposal.project.start_date, allocation_date.date())

    def test_fixed_date_allocation_sets_project_start_date_as_a_date(self):
        # Regression: Project.start_date is a DateField but the round's
        # allocation_date is a DateTimeField. allocate_proposal must coerce it,
        # otherwise the in-memory project carries a datetime and downstream
        # date comparisons (the order-created notification handler) crash with
        # a datetime-vs-date TypeError.
        new_proposal = factories.ProposalFactory(
            round=self.fixture.round,
            state=ProposalStates.IN_REVIEW,
            project=None,
        )
        new_proposal.round.allocation_date = timezone.now() + datetime.timedelta(
            weeks=1
        )
        new_proposal.round.save()
        models.CallWorkflowStep.objects.update_or_create(
            call=new_proposal.round.call,
            step="allocation_decision",
            defaults={"allocation_time": AllocationTimes.FIXED_DATE},
        )

        utils.allocate_proposal(new_proposal)

        # Check the in-memory value (a reload would coerce it regardless).
        start_date = new_proposal.project.start_date
        self.assertIsInstance(start_date, datetime.date)
        self.assertNotIsInstance(start_date, datetime.datetime)


@ddt
class RequestedResourceGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedResourceFactory.get_list_url(
            self.fixture.proposal
        )

    @data(
        "staff",
        "call_manager",
    )
    def test_requested_resource_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_call_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class RequestedResourceCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.requested_offering_accepted = self.fixture.requested_offering_accepted
        self.requested_offering = self.fixture.requested_offering
        self.url = factories.RequestedResourceFactory.get_list_url(self.proposal)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_add_resource_to_proposal(self, user):
        response = self.add_resource(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.RequestedResource.objects.filter(uuid=response.data["uuid"]).exists()
        )

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_add_resource_to_proposal(self, user):
        response = self.add_resource(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "staff",
    )
    def test_user_can_not_add_resource_to_not_draft_proposal(self, user):
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        response = self.add_resource(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(
        "staff",
    )
    def test_user_can_not_add_if_requested_offering_is_not_accepted(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        payload = {
            "requested_offering": self.requested_offering.uuid.hex,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def add_resource(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {"requested_offering_uuid": self.requested_offering_accepted.uuid.hex}

        return self.client.post(self.url, payload)


@ddt
class RequestedResourceUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.proposal = self.fixture.proposal
        self.url = factories.RequestedResourceFactory.get_url(
            self.proposal, self.requested_resource
        )

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_update_requested_resource(self, user):
        response = self.update_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_update_requested_resource(self, user):
        response = self.update_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "staff",
    )
    def test_user_can_not_update_not_draft_requested_resource(self, user):
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        response = self.update_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def update_requested_resource(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "description": "description",
            "requested_offering_uuid": self.requested_resource.requested_offering.uuid.hex,
        }
        response = self.client.patch(self.url, payload)
        self.requested_resource.refresh_from_db()
        return response


@ddt
class RequestedResourceDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.proposal = self.fixture.proposal
        self.url = factories.RequestedResourceFactory.get_url(
            self.proposal, self.requested_resource
        )

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_delete_requested_resource(self, user):
        response = self.delete_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_delete_requested_resource(self, user):
        response = self.delete_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "staff",
    )
    def test_user_can_not_delete_not_draft_requested_resource(self, user):
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        response = self.delete_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data(
        "owner",
        "customer_support",
    )
    def test_customer_user_can_not_delete_not_draft_requested_resource(self, user):
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        response = self.delete_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def delete_requested_resource(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)


class TaskTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.round = self.fixture.round

    def test_proposals_for_ended_rounds_should_be_cancelled(self):
        self.round.cutoff_time = timezone.now() + datetime.timedelta(days=1)
        self.round.save()
        tasks.proposals_for_ended_rounds_should_be_cancelled()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)

        self.round.cutoff_time = timezone.now() - datetime.timedelta(days=1)
        self.round.save()
        tasks.proposals_for_ended_rounds_should_be_cancelled()
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.CANCELED)

        from waldur_core.logging.models import Event

        self.assertTrue(Event.objects.filter(event_type="proposal_canceled").exists())

    @override_settings(task_always_eager=True)
    def test_notifications_for_cancelled_proposals(self):
        structure_factories.NotificationFactory(
            key="proposal.proposal_cancelled",
        )
        self.round.cutoff_time = timezone.now() - datetime.timedelta(days=1)
        self.round.save()
        tasks.proposals_for_ended_rounds_should_be_cancelled()
        self.proposal.refresh_from_db()

        # Verify that notification email has been sent to proposal creator
        # in fixtures.py there are two proposals belong to this round; therefore, there are two emails in the mail outbox
        self.assertEqual(len(mail.outbox), 2)

        # Check that both expected email recipients are present (order doesn't matter)
        email_recipients = [mail.to for mail in mail.outbox]
        expected_recipients = [
            [self.proposal.created_by.email],
            [self.fixture.proposal_submitted.created_by.email],
        ]

        self.assertEqual(sorted(email_recipients), sorted(expected_recipients))

        # Find the email for self.proposal and verify its content
        proposal_email = None
        for email in mail.outbox:
            if email.to == [self.proposal.created_by.email]:
                proposal_email = email
                break

        self.assertIsNotNone(proposal_email, "Email for self.proposal not found")
        body = proposal_email.body
        self.assertIn(
            f'Your proposal "{self.proposal.name}" in call "{self.proposal.round.call.name}" has been canceled',
            body,
        )
        self.assertIn("Cancellation details:", body)
