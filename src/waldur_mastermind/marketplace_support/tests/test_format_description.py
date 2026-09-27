import datetime

from django.test import TestCase

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import BillingTypes, OrderTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import utils
from waldur_mastermind.proposal.tests import factories as proposal_factories


class FormatCreateDescriptionTest(TestCase):
    def test_start_date_included_when_set(self):
        order = marketplace_factories.OrderFactory(
            start_date=datetime.date(2026, 6, 1),
        )
        description = utils.format_create_description(order)
        self.assertIn("Start date: June 1, 2026", description)

    def test_end_date_included_when_set(self):
        order = marketplace_factories.OrderFactory()
        order.resource.end_date = datetime.date(2026, 12, 31)
        order.resource.save(update_fields=["end_date"])
        description = utils.format_create_description(order)
        self.assertIn("End date: Dec. 31, 2026", description)

    def test_dates_included_when_both_set(self):
        order = marketplace_factories.OrderFactory(
            start_date=datetime.date(2026, 6, 1),
        )
        order.resource.end_date = datetime.date(2026, 12, 31)
        order.resource.save(update_fields=["end_date"])
        description = utils.format_create_description(order)
        self.assertIn("Start date:", description)
        self.assertIn("End date:", description)

    def test_dates_not_included_when_not_set(self):
        order = marketplace_factories.OrderFactory(
            start_date=None,
        )
        order.resource.end_date = None
        order.resource.save(update_fields=["end_date"])
        description = utils.format_create_description(order)
        self.assertNotIn("Start date:", description)
        self.assertNotIn("End date:", description)

    def test_restoration_note_included_for_restore_order(self):
        order = marketplace_factories.OrderFactory(type=OrderTypes.RESTORE)
        description = utils.format_create_description(order)
        self.assertIn(
            "This is a restoration request for a previously terminated resource.",
            description,
        )

    def test_restoration_note_not_included_for_create_order(self):
        order = marketplace_factories.OrderFactory(type=OrderTypes.CREATE)
        description = utils.format_create_description(order)
        self.assertNotIn("restoration request", description)

    def test_create_description_without_plan(self):
        order = marketplace_factories.OrderFactory(plan=None, limits={"cpu": 10})
        marketplace_factories.OfferingComponentFactory(
            offering=order.offering,
            type="cpu",
            name="CPU",
            measured_unit="cores",
            billing_type=BillingTypes.LIMIT,
        )
        description = utils.format_create_description(order)
        self.assertIn("Plan details:\n    Plan: none", description)
        self.assertIn(f"Resource UUID: {order.resource.uuid}", description)
        self.assertIn("CPU (cpu): 10 cores", description)
        self.assertIn(f"Email: {order.created_by.email}", description)


class FormatDeleteDescriptionTest(TestCase):
    def test_delete_description_without_plan(self):
        order = marketplace_factories.OrderFactory(type=OrderTypes.TERMINATE, plan=None)
        order.resource.plan = None
        order.resource.save(update_fields=["plan"])
        description = utils.format_delete_description(order)
        self.assertIn("Plan: none", description)
        self.assertIn(
            f"Marketplace resource UUID: {order.resource.uuid.hex}", description
        )


class FormatCreateDescriptionApplicantTest(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.proposal = proposal_factories.ProposalFactory(
            project=self.fixture.project, name="Cold storage for imaging"
        )
        self.applicant = self.proposal.created_by

    def make_allocated_order(self):
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project, placed_automatically=True
        )
        proposal_factories.RequestedResourceFactory(
            proposal=self.proposal, resource=order.resource
        )
        return order

    def test_applicant_and_proposal_included_for_allocated_order(self):
        order = self.make_allocated_order()
        description = utils.format_create_description(order)
        self.assertIn(
            f"Applicant: {self.applicant.full_name} (e-mail: {self.applicant.email})",
            description,
        )
        self.assertIn("Proposal: Cold storage for imaging (", description)
        self.assertIn(f"proposals/{self.proposal.uuid.hex}/", description)

    def test_applicant_not_included_for_order_placed_by_a_person(self):
        # A proposal in the same project does not make every order in it an
        # allocation: only the resource the proposal granted is linked to it.
        order = marketplace_factories.OrderFactory(project=self.fixture.project)
        description = utils.format_create_description(order)
        self.assertNotIn("Applicant:", description)
        self.assertNotIn("Proposal:", description)

    def test_proposal_included_when_applicant_is_gone(self):
        self.proposal.created_by = None
        self.proposal.save(update_fields=["created_by"])
        order = self.make_allocated_order()
        description = utils.format_create_description(order)
        self.assertNotIn("Applicant:", description)
        self.assertIn("Proposal: Cold storage for imaging (", description)


class FormatCreateDescriptionTeamTest(TestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.manager = self.fixture.manager
        self.member = self.fixture.member
        self.offering = marketplace_factories.OfferingFactory()

    def make_order(self, track_membership):
        self.offering.plugin_options = {
            "enable_issues_for_membership_changes": track_membership
        }
        self.offering.save()
        return marketplace_factories.OrderFactory(
            project=self.fixture.project, offering=self.offering
        )

    def test_team_included_when_offering_tracks_membership(self):
        description = utils.format_create_description(self.make_order(True))
        self.assertIn("Project team:", description)
        for user, role in (
            (self.manager, "PROJECT.MANAGER"),
            (self.member, "PROJECT.MEMBER"),
        ):
            self.assertIn(
                f"- {user.full_name} (e-mail: {user.email}, "
                f"username: {user.username}), role: {role}",
                description,
            )

    def test_team_not_included_when_offering_does_not_track_membership(self):
        description = utils.format_create_description(self.make_order(False))
        self.assertNotIn("Project team:", description)
        self.assertNotIn(self.member.username, description)

    def test_inactive_members_not_listed(self):
        self.fixture.project.remove_user(self.member)
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"])
        description = utils.format_create_description(self.make_order(True))
        self.assertNotIn("Project team:", description)
