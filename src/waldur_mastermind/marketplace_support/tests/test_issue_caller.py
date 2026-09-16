from rest_framework import exceptions as rf_exceptions

from waldur_core.core import utils as core_utils
from waldur_core.permissions.fixtures import CallRole, CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import tasks as marketplace_support_tasks
from waldur_mastermind.marketplace_support.utils import get_order_issue
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories as proposal_factories
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests.base import BaseTest


class IssueCallerTest(BaseTest):
    """Who the helpdesk request is raised on behalf of.

    The caller is the person the service desk talks to and who Waldur's ticket
    notifications are addressed to, so it has to be a real user with an email.
    Automated flows place orders as a robot that has none, which used to fail
    the order outright (issue #449).

    Reading the ticket in the customer portal is a separate question: these
    tickets carry a project and a customer, and that scope decides who sees
    them. Being the caller does not add access.
    """

    def setUp(self):
        super().setUp()
        self.offering = marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING)
        self.project = structure_factories.ProjectFactory()
        self.robot = core_utils.get_system_robot()

    def process(self, created_by, actor=None):
        """``actor`` is who runs the processing, which is not always the person
        who placed the order -- an approver or the robot usually is."""
        order = marketplace_factories.OrderFactory(
            offering=self.offering,
            project=self.project,
            created_by=created_by,
            attributes={"name": "item_name", "description": "Description"},
            state=OrderStates.EXECUTING,
        )
        marketplace_utils.process_order(order, actor or created_by)
        order.refresh_from_db()
        return order

    def test_order_creator_is_the_caller_when_they_have_an_email(self):
        creator = structure_factories.UserFactory()

        order = self.process(creator)

        self.assertEqual(get_order_issue(order).caller, creator)

    def test_robot_creator_falls_back_to_the_project_manager(self):
        manager = structure_factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)

        order = self.process(self.robot)

        self.assertEqual(get_order_issue(order).caller, manager)

    def test_project_manager_is_preferred_over_admin_and_member(self):
        member = structure_factories.UserFactory()
        admin = structure_factories.UserFactory()
        manager = structure_factories.UserFactory()
        self.project.add_user(member, ProjectRole.MEMBER)
        self.project.add_user(admin, ProjectRole.ADMIN)
        self.project.add_user(manager, ProjectRole.MANAGER)

        order = self.process(self.robot)

        self.assertEqual(get_order_issue(order).caller, manager)

    def test_admin_is_preferred_over_member(self):
        member = structure_factories.UserFactory()
        admin = structure_factories.UserFactory()
        self.project.add_user(member, ProjectRole.MEMBER)
        self.project.add_user(admin, ProjectRole.ADMIN)

        order = self.process(self.robot)

        self.assertEqual(get_order_issue(order).caller, admin)

    def test_candidate_without_an_email_is_skipped(self):
        # An SSO user provisioned without an email claim is as unusable as the
        # robot: the helpdesk rejects the request either way.
        silent_manager = structure_factories.UserFactory(email="")
        admin = structure_factories.UserFactory()
        self.project.add_user(silent_manager, ProjectRole.MANAGER)
        self.project.add_user(admin, ProjectRole.ADMIN)

        order = self.process(self.robot)

        self.assertEqual(get_order_issue(order).caller, admin)

    def test_inactive_candidate_is_skipped(self):
        # An inactive caller is refused by the backend with SupportUserInactive.
        inactive_manager = structure_factories.UserFactory()
        admin = structure_factories.UserFactory()
        self.project.add_user(inactive_manager, ProjectRole.MANAGER)
        self.project.add_user(admin, ProjectRole.ADMIN)
        inactive_manager.is_active = False
        inactive_manager.save()

        order = self.process(self.robot)

        self.assertEqual(get_order_issue(order).caller, admin)

    def test_customer_owner_is_used_when_the_project_has_no_members(self):
        owner = structure_factories.UserFactory()
        self.project.customer.add_user(owner, CustomerRole.OWNER)

        order = self.process(self.robot)

        self.assertEqual(get_order_issue(order).caller, owner)

    def test_email_less_human_creator_also_falls_back(self):
        creator = structure_factories.UserFactory(email="")
        manager = structure_factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)

        order = self.process(creator)

        self.assertEqual(get_order_issue(order).caller, manager)

    def test_deactivated_creator_falls_back(self):
        # The backend refuses an inactive caller with SupportUserInactive, so
        # having an email address is not on its own enough.
        creator = structure_factories.UserFactory()
        manager = structure_factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)
        creator.is_active = False
        creator.save()

        # Processed by the robot: an inactive user cannot be the actor either,
        # the internal request rejects them with 401 before create_issue runs.
        order = self.process(creator, actor=self.robot)

        self.assertEqual(get_order_issue(order).caller, manager)

    def test_order_errs_legibly_when_nobody_can_be_reached(self):
        order = self.process(self.robot)

        self.assertEqual(order.state, OrderStates.ERRED)
        # Names the project, rather than the opaque backend message that sent
        # the customer in #449 looking for a missing email address.
        self.assertIn(self.project.name, order.error_message)
        self.assertNotIn("does not have email", order.error_message)
        self.assertFalse(support_models.Issue.objects.exists())
        # The resource is marked too, not just the order: the paths that call
        # create_issue outside process_order have no error handling of their
        # own and would otherwise leave it in CREATING.
        order.resource.refresh_from_db()
        self.assertEqual(order.resource.state, ResourceStates.ERRED)

    def test_the_choice_is_stable_across_orders(self):
        # A project with several managers must not hand consecutive tickets to
        # different people.
        for _ in range(3):
            self.project.add_user(structure_factories.UserFactory(), ProjectRole.MEMBER)

        callers = {get_order_issue(self.process(self.robot)).caller for _ in range(3)}

        self.assertEqual(len(callers), 1)

    def test_robot_remains_the_recorded_order_creator(self):
        # Only the ticket caller is substituted: created_by is the audit trail,
        # and the approval gate reads its is_staff.
        manager = structure_factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)

        order = self.process(self.robot)

        self.assertEqual(order.created_by, self.robot)
        self.assertEqual(get_order_issue(order).caller, manager)

    def test_system_robot_still_has_no_email(self):
        # The premise of #449. If this ever stops holding, the fallback is no
        # longer exercised by the flows that need it.
        self.assertEqual(self.robot.email, "")


class CallConfiguredIssueCallerTest(BaseTest):
    """A call decides who its allocated resources' tickets belong to.

    Proposal allocation places orders as the robot, so without this the ticket
    would land on whichever project role happened to sort first.
    """

    def setUp(self):
        super().setUp()
        self.proposal_fixture = proposal_fixtures.ProposalFixture()
        self.call = self.proposal_fixture.call
        self.project = self.proposal_fixture.proposal_project
        self.proposal = self.proposal_fixture.proposal
        self.applicant = self.proposal.created_by
        self.offering = marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING)
        self.robot = core_utils.get_system_robot()

        # Someone the generic role fallback would otherwise pick, so each test
        # shows the call's setting winning rather than coinciding.
        self.project_manager = structure_factories.UserFactory()
        self.project.add_user(self.project_manager, ProjectRole.MANAGER)

    def configure(self, caller, user=None):
        self.call.support_ticket_caller = caller
        self.call.support_ticket_caller_user = user
        self.call.save()

    def process(self, proposal=None):
        order = marketplace_factories.OrderFactory(
            offering=self.offering,
            project=self.project,
            created_by=self.robot,
            attributes={"name": "item_name", "description": "Description"},
            state=OrderStates.EXECUTING,
        )
        # Allocation records which proposal produced the resource, and the
        # resolver follows that link rather than guessing from the project.
        proposal_factories.RequestedResourceFactory(
            proposal=proposal or self.proposal,
            resource=order.resource,
            created_by=self.applicant,
        )
        marketplace_utils.process_order(order, self.robot)
        order.refresh_from_db()
        return order

    def test_applicant_is_the_default(self):
        self.assertEqual(
            self.call.support_ticket_caller, models.Call.TicketCaller.APPLICANT
        )

        self.assertEqual(get_order_issue(self.process()).caller, self.applicant)

    def test_call_can_route_to_the_project_manager(self):
        self.configure(models.Call.TicketCaller.PROJECT_MANAGER)

        self.assertEqual(get_order_issue(self.process()).caller, self.project_manager)

    def test_call_can_route_to_the_call_manager(self):
        call_manager = structure_factories.UserFactory()
        self.call.add_user(call_manager, CallRole.MANAGER)
        self.configure(models.Call.TicketCaller.CALL_MANAGER)

        self.assertEqual(get_order_issue(self.process()).caller, call_manager)

    def test_call_can_route_to_a_named_contact(self):
        grants_office = structure_factories.UserFactory()
        self.configure(models.Call.TicketCaller.SPECIFIC_USER, grants_office)

        self.assertEqual(get_order_issue(self.process()).caller, grants_office)

    def test_unreachable_configured_caller_falls_through_to_the_role_chain(self):
        # A named contact who lost their email address must not fail the order
        # when somebody on the project can still take the ticket.
        self.configure(
            models.Call.TicketCaller.SPECIFIC_USER,
            structure_factories.UserFactory(email=""),
        )

        self.assertEqual(get_order_issue(self.process()).caller, self.project_manager)

    def test_specific_user_with_no_contact_named_falls_through(self):
        # The mode is selectable before the contact is picked, so this state is
        # reachable through the settings page.
        self.configure(models.Call.TicketCaller.SPECIFIC_USER, None)

        self.assertEqual(get_order_issue(self.process()).caller, self.project_manager)

    def test_applicant_without_an_email_falls_through_to_the_role_chain(self):
        self.applicant.email = ""
        self.applicant.save()

        self.assertEqual(get_order_issue(self.process()).caller, self.project_manager)

    def test_the_producing_proposal_decides_not_the_lowest_numbered_one(self):
        # The fixture hangs a second proposal off the same project. Resolving
        # by project would pick whichever sorts first; the resource link makes
        # it unambiguous.
        other = self.proposal_fixture.proposal_submitted
        self.assertEqual(other.project, self.project)
        self.assertNotEqual(other.created_by, self.applicant)

        order = self.process(proposal=other)

        self.assertEqual(get_order_issue(order).caller, other.created_by)

    def test_unreachable_configured_caller_is_logged(self):
        self.configure(
            models.Call.TicketCaller.SPECIFIC_USER,
            structure_factories.UserFactory(email=""),
        )

        with self.assertLogs(
            "waldur_mastermind.marketplace_support.utils", level="WARNING"
        ) as logs:
            self.process()

        self.assertTrue(
            any("Falling back to the roles" in line for line in logs.output),
            logs.output,
        )

    def test_order_placed_by_a_person_ignores_the_call_setting(self):
        # The call only decides who stands in for an automated order.
        self.configure(models.Call.TicketCaller.SPECIFIC_USER)
        buyer = structure_factories.UserFactory()

        order = marketplace_factories.OrderFactory(
            offering=self.offering,
            project=self.project,
            created_by=buyer,
            attributes={"name": "item_name", "description": "Description"},
            state=OrderStates.EXECUTING,
        )
        marketplace_utils.process_order(order, buyer)

        self.assertEqual(get_order_issue(order).caller, buyer)


class PendingOrderIssueCallerTest(BaseTest):
    """A deferred-start order raises its ticket through the same resolver.

    ``create_issue_for_pending_order`` runs outside ``process_order`` and has no
    exception handling of its own, so whatever ``create_issue`` does to the
    resource before raising is the only thing that stops a PENDING_PROJECT
    order sitting in CREATING for good.
    """

    def setUp(self):
        super().setUp()
        self.offering = marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING)
        self.project = structure_factories.ProjectFactory()
        self.robot = core_utils.get_system_robot()

    def pending_order(self):
        return marketplace_factories.OrderFactory(
            offering=self.offering,
            project=self.project,
            created_by=self.robot,
            attributes={"name": "item_name", "description": "Description"},
            state=OrderStates.PENDING_PROJECT,
        )

    def run_task(self, order):
        marketplace_support_tasks.create_issue_for_pending_order(
            core_utils.serialize_instance(order)
        )

    def test_robot_creator_falls_back_to_the_project_manager(self):
        manager = structure_factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)
        order = self.pending_order()

        self.run_task(order)

        self.assertEqual(get_order_issue(order).caller, manager)

    def test_resource_is_erred_when_nobody_can_be_reached(self):
        order = self.pending_order()

        with self.assertRaises(rf_exceptions.ValidationError):
            self.run_task(order)

        order.resource.refresh_from_db()
        self.assertEqual(order.resource.state, ResourceStates.ERRED)
        self.assertFalse(support_models.Issue.objects.exists())
