from rest_framework import exceptions as rf_exceptions

from waldur_core.core import utils as core_utils
from waldur_core.permissions.fixtures import CallRole, CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
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
from waldur_mastermind.proposal import utils as proposal_utils
from waldur_mastermind.proposal.enums import ProposalStates
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

    def test_the_recorded_order_creator_is_left_alone(self):
        # Only the ticket caller is substituted. created_by is the audit
        # trail; the sweeps that still place orders as a robot keep saying so.
        manager = structure_factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)

        order = self.process(self.robot)

        self.assertEqual(order.created_by, self.robot)
        self.assertEqual(get_order_issue(order).caller, manager)

    def test_system_robot_still_has_no_email(self):
        # The premise of #449. If this ever stops holding, the fallback is no
        # longer exercised by the flows that need it.
        self.assertEqual(self.robot.email, "")


class AllocatedOrderIssueCallerTest(BaseTest):
    """A call's order author decides who its tickets are raised for.

    The call is not consulted here at all any more: allocation puts the
    configured person in ``created_by`` and the caller follows from that, the
    same way it does for an order somebody placed themselves. These run
    through ``allocate_proposal`` rather than building the order by hand, so
    they would notice if that stopped being true.
    """

    def setUp(self):
        super().setUp()
        self.proposal_fixture = proposal_fixtures.ProposalFixture()
        self.call = self.proposal_fixture.call
        self.proposal = self.proposal_fixture.proposal
        self.applicant = self.proposal.created_by
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.project = None
        self.proposal.save()

        # The fixture's requested resource already hangs off an accepted
        # requested offering, so allocation picks it up as it stands.
        offering = self.proposal_fixture.offering
        offering.type = SUPPORT_OFFERING
        offering.save(update_fields=["type"])

    def configure(self, author, user=None):
        self.call.order_author = author
        self.call.order_author_user = user
        self.call.save()

    def allocate(self):
        proposal_utils.allocate_proposal(
            self.proposal, approved_by=self.proposal_fixture.staff
        )
        self.proposal.refresh_from_db()
        resource = self.proposal.requestedresource_set.first().resource
        return marketplace_models.Order.objects.get(resource=resource)

    def process(self, order):
        # Allocation queues the processing on a celery task that tests do not
        # run. Drive it with the identity the handler would have passed.
        marketplace_utils.process_order(
            order, marketplace_utils.get_order_processing_user(order)
        )
        order.refresh_from_db()
        return order

    def allocate_and_process(self):
        return self.process(self.allocate())

    def test_applicant_is_the_default(self):
        self.assertEqual(self.call.order_author, models.Call.OrderAuthor.APPLICANT)

        order = self.allocate_and_process()

        self.assertEqual(order.created_by, self.applicant)
        self.assertEqual(get_order_issue(order).caller, self.applicant)

    def test_call_can_route_to_the_call_manager(self):
        call_manager = structure_factories.UserFactory()
        self.call.add_user(call_manager, CallRole.MANAGER)
        self.configure(models.Call.OrderAuthor.CALL_MANAGER)

        order = self.allocate_and_process()

        self.assertEqual(order.created_by, call_manager)
        self.assertEqual(get_order_issue(order).caller, call_manager)

    def test_call_can_route_to_a_named_contact(self):
        grants_office = structure_factories.UserFactory()
        self.configure(models.Call.OrderAuthor.SPECIFIC_USER, grants_office)

        order = self.allocate_and_process()

        self.assertEqual(order.created_by, grants_office)
        self.assertEqual(get_order_issue(order).caller, grants_office)

    def test_applicant_without_an_email_falls_through_to_the_role_chain(self):
        # Allocation keeps an unreachable applicant as the author -- they
        # wrote the proposal, address or not -- so the ticket is what has to
        # find somebody else.
        self.applicant.email = ""
        self.applicant.save(update_fields=["email"])

        order = self.allocate()
        manager = structure_factories.UserFactory()
        order.project.add_user(manager, ProjectRole.MANAGER)
        self.process(order)

        self.assertEqual(order.created_by, self.applicant)
        self.assertEqual(get_order_issue(order).caller, manager)


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
