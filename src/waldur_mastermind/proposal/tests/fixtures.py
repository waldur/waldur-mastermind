import datetime

from django.utils.functional import cached_property

from waldur_core.permissions import enums
from waldur_core.permissions import models as permissions_models
from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.tests import factories as proposal_factories


class ProposalFixture(structure_fixtures.CustomerFixture):
    def __init__(self):
        self.requested_offering
        self.new_call
        self.requested_resource
        self.reviewer_1
        self.reviewer_2
        self.review

        permissions_models.RolePermission.objects.get_or_create(
            role=CallRole.MANAGER,
            permission=enums.PermissionEnum.APPROVE_AND_REJECT_PROPOSALS,
        )

    @cached_property
    def manager(self):
        return proposal_factories.CallManagingOrganisationFactory(
            customer=self.customer,
            description="Manager's description",
        )

    @cached_property
    def call(self):
        return proposal_factories.CallFactory(
            manager=self.manager,
            state=proposal_models.Call.States.ACTIVE,
            created_by=self.owner,
        )

    @cached_property
    def new_call(self):
        return proposal_factories.CallFactory(
            manager=self.manager, created_by=self.owner
        )

    @cached_property
    def offering_fixture(self):
        return marketplace_fixtures.MarketplaceFixture()

    @cached_property
    def requested_offering(self):
        return proposal_factories.RequestedOfferingFactory(
            call=self.call,
            state=proposal_models.RequestedOffering.States.REQUESTED,
            created_by=self.owner,
            offering=self.offering,
        )

    @cached_property
    def requested_offering_accepted(self):
        return proposal_factories.RequestedOfferingFactory(
            call=self.call,
            state=proposal_models.RequestedOffering.States.ACCEPTED,
            created_by=self.owner,
            offering=self.offering,
        )

    @cached_property
    def offering(self):
        return self.offering_fixture.offering

    @cached_property
    def resource(self):
        return self.offering_fixture.resource

    @cached_property
    def offering_owner(self):
        return self.offering_fixture.offering_owner

    @cached_property
    def round(self):
        return proposal_factories.RoundFactory(
            call=self.call,
            start_time=datetime.date.today(),
            cutoff_time=datetime.date.today() + datetime.timedelta(days=10),
            minimum_number_of_reviewers=1,
        )

    @cached_property
    def new_round(self):
        return proposal_factories.RoundFactory(
            call=self.call,
            start_time=datetime.date.today(),
            cutoff_time=datetime.date.today() + datetime.timedelta(days=10),
        )

    @cached_property
    def proposal(self):
        return proposal_factories.ProposalFactory(
            round=self.round,
            project=self.proposal_project,
        )

    @cached_property
    def proposal_project(self):
        return structure_factories.ProjectFactory(customer=self.customer)

    @cached_property
    def proposal_submitted(self):
        return proposal_factories.ProposalFactory(
            round=self.round,
            state=proposal_models.Proposal.States.SUBMITTED,
            project=self.proposal_project,
        )

    @cached_property
    def proposal_submitted_project(self):
        return structure_factories.ProjectFactory(customer=self.customer)

    @cached_property
    def review(self):
        return proposal_factories.ReviewFactory(
            proposal=self.proposal_submitted, reviewer=self.reviewer_1
        )

    @cached_property
    def proposal_creator(self):
        return self.proposal.created_by

    @cached_property
    def proposal_submitted_creator(self):
        return self.proposal_submitted.created_by

    @cached_property
    def requested_resource(self):
        return proposal_factories.RequestedResourceFactory(
            requested_offering=self.requested_offering_accepted, proposal=self.proposal
        )

    @cached_property
    def reviewer_1(self):
        user = structure_factories.UserFactory()
        role = CallRole.REVIEWER
        permissions_utils.add_user(self.call, user, role)
        return user

    @cached_property
    def reviewer_2(self):
        user = structure_factories.UserFactory()
        role = CallRole.REVIEWER
        permissions_utils.add_user(self.call, user, role)
        return user

    @cached_property
    def call_manager(self):
        user = structure_factories.UserFactory()
        role = CallRole.MANAGER
        permissions_utils.add_user(self.call, user, role)
        return user
