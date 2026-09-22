from unittest import mock

from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import get_system_robot
from waldur_core.permissions.fixtures import CallRole, ProjectRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    SUPPORT_OFFERING,
    OrderStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models, utils
from waldur_mastermind.proposal.enums import (
    OrderAuthors,
    ProposalStates,
    RequestedOfferingStates,
)
from waldur_mastermind.proposal.tests import factories as proposal_factories
from waldur_mastermind.proposal.tests import fixtures


class OrderAuthorTest(test.APITestCase):
    """Whose name the orders a call places when it grants resources carry.

    Before this the orders were placed by the system robot, whose staff flag
    cleared the consumer step and whose missing email address broke every
    offering fulfilled by raising a helpdesk ticket (#449). The author is now
    recorded on the order, the accepting call manager is recorded as the
    consumer reviewer, and the robot's only remaining job is carrying the
    order out.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal
        self.applicant = self.proposal.created_by
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.project = None
        self.proposal.save()
        # Support falls through to the catch-all in the provider gate, so
        # allocation takes the order all the way rather than parking it.
        offering = self.fixture.offering
        offering.type = SUPPORT_OFFERING
        offering.save(update_fields=["type"])

    def configure(self, author, user=None):
        self.call.order_author = author
        self.call.order_author_user = user
        self.call.save()

    def allocate(self, approved_by=None):
        utils.allocate_proposal(
            self.proposal, approved_by=approved_by or self.fixture.staff
        )
        self.proposal.refresh_from_db()
        resource = self.proposal.requestedresource_set.first().resource
        return marketplace_models.Order.objects.get(resource=resource)

    def test_applicant_is_the_default(self):
        self.assertEqual(self.call.order_author, OrderAuthors.APPLICANT)

        self.assertEqual(self.allocate().created_by, self.applicant)

    def test_call_can_attribute_orders_to_the_project_manager(self):
        # The role mapping runs before the orders are placed, so the project
        # already has its people by the time the author is resolved.
        models.ProposalProjectRoleMapping.objects.create(
            call=self.call,
            proposal_role=ProposalRole.MEMBER,
            project_role=ProjectRole.MANAGER,
        )
        member = structure_factories.UserFactory()
        self.proposal.add_user(member, ProposalRole.MEMBER)
        self.configure(OrderAuthors.PROJECT_MANAGER)

        self.assertEqual(self.allocate().created_by, member)

    def test_call_can_attribute_orders_to_the_call_manager(self):
        call_manager = structure_factories.UserFactory()
        self.call.add_user(call_manager, CallRole.MANAGER)
        self.configure(OrderAuthors.CALL_MANAGER)

        self.assertEqual(self.allocate().created_by, call_manager)

    def test_call_can_attribute_orders_to_a_named_contact(self):
        grants_office = structure_factories.UserFactory()
        self.configure(OrderAuthors.SPECIFIC_USER, grants_office)

        self.assertEqual(self.allocate().created_by, grants_office)

    def test_named_contact_left_unset_falls_back_to_the_applicant(self):
        # The mode is selectable before the contact is picked, so this state
        # is reachable straight from the call settings page.
        self.configure(OrderAuthors.SPECIFIC_USER, None)

        self.assertEqual(self.allocate().created_by, self.applicant)

    def test_role_nobody_holds_falls_back_to_the_applicant(self):
        self.configure(OrderAuthors.CALL_MANAGER)

        self.assertEqual(self.allocate().created_by, self.applicant)

    def test_fallback_is_logged(self):
        self.configure(OrderAuthors.CALL_MANAGER)

        with self.assertLogs(
            "waldur_mastermind.proposal.utils", level="WARNING"
        ) as logs:
            self.allocate()

        self.assertTrue(
            any("Falling back to the applicant" in line for line in logs.output),
            logs.output,
        )

    def test_named_contact_without_an_email_falls_back_to_the_applicant(self):
        # Naming somebody is how a call says "tell this person about the
        # order". Someone who cannot be told is no use as the author, so the
        # applicant takes it back.
        grants_office = structure_factories.UserFactory(email="")
        self.configure(OrderAuthors.SPECIFIC_USER, grants_office)

        self.assertEqual(self.allocate().created_by, self.applicant)

    def test_role_holder_without_an_email_is_skipped(self):
        silent = structure_factories.UserFactory(email="")
        reachable = structure_factories.UserFactory()
        self.call.add_user(silent, CallRole.MANAGER)
        self.call.add_user(reachable, CallRole.MANAGER)
        self.configure(OrderAuthors.CALL_MANAGER)

        self.assertEqual(self.allocate().created_by, reachable)

    def test_an_applicant_without_an_email_is_still_the_author(self):
        # Unlike a configured choice: they authored the proposal either way,
        # and resolve_issue_caller still finds somebody to raise the ticket.
        self.applicant.email = ""
        self.applicant.save(update_fields=["email"])

        self.assertEqual(self.allocate().created_by, self.applicant)

    def test_inactive_named_contact_falls_back_to_the_applicant(self):
        grants_office = structure_factories.UserFactory(is_active=False)
        self.configure(OrderAuthors.SPECIFIC_USER, grants_office)

        self.assertEqual(self.allocate().created_by, self.applicant)

    def test_every_order_in_one_allocation_shares_the_author(self):
        # A second granted resource on a genuinely different offering -- the
        # fixture's own requested resource already uses
        # `requested_offering_accepted`, so reusing that would give two
        # resources on one offering and prove nothing about drift.
        second = proposal_factories.RequestedOfferingFactory(
            call=self.call,
            state=RequestedOfferingStates.ACCEPTED,
            offering=marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING),
        )
        models.RequestedResource.objects.create(
            proposal=self.proposal,
            requested_offering=second,
            created_by=self.applicant,
        )

        self.allocate()

        resources = [r.resource for r in self.proposal.requestedresource_set.all()]
        self.assertEqual(len(resources), 2)
        authors = set(
            marketplace_models.Order.objects.filter(resource__in=resources).values_list(
                "created_by", flat=True
            )
        )
        self.assertEqual(authors, {self.applicant.id})


class AllocatedOrderApprovalRecordTest(test.APITestCase):
    """Accepting the proposal is the consumer-side decision on the order.

    The granted project belongs to the call's managing organisation, so
    whoever accepted the proposal decided on that organisation's behalf. That
    is recorded rather than bypassed with a staff creator.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.project = None
        self.proposal.save()
        offering = self.fixture.offering
        offering.type = SUPPORT_OFFERING
        offering.save(update_fields=["type"])

    def allocate(self, approved_by=None):
        utils.allocate_proposal(self.proposal, approved_by=approved_by)
        self.proposal.refresh_from_db()
        resource = self.proposal.requestedresource_set.first().resource
        return marketplace_models.Order.objects.get(resource=resource)

    def test_the_accepting_user_is_recorded_as_the_consumer_reviewer(self):
        approver = self.fixture.staff

        order = self.allocate(approved_by=approver)

        self.assertEqual(order.consumer_reviewed_by, approver)
        self.assertIsNotNone(order.consumer_reviewed_at)
        self.assertTrue(order.placed_automatically)

    @mock.patch.object(
        marketplace_models.Order,
        "review_by_consumer",
        autospec=True,
        side_effect=marketplace_models.Order.review_by_consumer,
    )
    def test_a_recorded_review_is_not_written_again(self, mock_review):
        # notify_approvers_when_order_is_created used to call this
        # unconditionally. It overwrites both fields and saves every column,
        # so it discarded the timestamp allocation had just set and cost an
        # extra full-row UPDATE per granted resource.
        order = self.allocate(approved_by=self.fixture.staff)

        mock_review.assert_not_called()
        self.assertEqual(order.consumer_reviewed_by, self.fixture.staff)
        self.assertIsNotNone(order.consumer_reviewed_at)

    def test_an_automatic_acceptance_records_the_robot(self):
        # The workflow terminal passes the robot when no human advanced it.
        order = self.allocate(approved_by=None)

        self.assertEqual(order.consumer_reviewed_by, get_system_robot())

    def test_the_consumer_step_is_cleared_without_a_staff_creator(self):
        call_manager = structure_factories.UserFactory()
        self.fixture.call.add_user(call_manager, CallRole.MANAGER)
        self.fixture.call.order_author = OrderAuthors.CALL_MANAGER
        self.fixture.call.save()

        order = self.allocate(approved_by=call_manager)

        self.assertFalse(order.created_by.is_staff)
        self.assertFalse(order.consumer_reviewed_by.is_staff)
        self.assertEqual(order.state, OrderStates.EXECUTING)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order")
    def test_the_order_is_carried_out_with_system_authority(self, mock_process_order):
        # The author may hold no role on the granted project at all -- a
        # grants office typically does not -- and processing replays the
        # plugin's own viewset as whoever is passed here.
        grants_office = structure_factories.UserFactory()
        self.fixture.call.order_author = OrderAuthors.SPECIFIC_USER
        self.fixture.call.order_author_user = grants_office
        self.fixture.call.save()

        with self.captureOnCommitCallbacks(execute=True):
            order = self.allocate(approved_by=self.fixture.staff)

        self.assertEqual(order.created_by, grants_office)
        self.assertEqual(
            marketplace_utils.get_order_processing_user(order), get_system_robot()
        )
        (_, serialized_user), _ = mock_process_order.delay.call_args
        self.assertEqual(
            core_utils.deserialize_instance(serialized_user), get_system_robot()
        )

    def test_a_remote_offering_still_goes_to_its_provider(self):
        # order_should_not_be_reviewed_by_provider reads the consumer reviewer,
        # and for a REMOTE offering waves the order through only when that
        # person has owner or service-manager access on the *provider's*
        # organisation. The staff robot always did; a call manager does not,
        # so the provider gets its say. Deployments that do not want that set
        # auto_approve_remote_orders on the offering.
        offering = self.fixture.offering
        offering.type = REMOTE_OFFERING
        offering.save(update_fields=["type"])
        call_manager = structure_factories.UserFactory()
        self.fixture.call.add_user(call_manager, CallRole.MANAGER)

        order = self.allocate(approved_by=call_manager)

        self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)

    @mock.patch("waldur_mastermind.marketplace.tasks.notify_about_new_order")
    def test_allocated_orders_are_not_announced_as_new_orders(self, mock_notify):
        # One mail per granted resource on allocation day is exactly the bulk
        # the guard in notify_recipients_when_order_is_created exists to avoid.
        offering = self.fixture.offering
        offering.secret_options = {"order_notification_emails": ["ops@example.com"]}
        offering.save(update_fields=["secret_options"])

        with self.captureOnCommitCallbacks(execute=True):
            self.allocate(approved_by=self.fixture.staff)

        mock_notify.delay.assert_not_called()
