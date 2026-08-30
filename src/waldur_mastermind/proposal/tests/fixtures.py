import datetime

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.utils.functional import cached_property

from waldur_core.permissions import enums
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
)
from waldur_mastermind.proposal.tests import factories as proposal_factories


class ProposalFixture(structure_fixtures.CustomerFixture):
    def __init__(self):
        self.requested_offering
        self.new_call
        self.requested_resource
        self.reviewer_1
        self.reviewer_2
        self.review
        self.round

        for perm in (
            enums.PermissionEnum.APPROVE_AND_REJECT_PROPOSALS,
            enums.PermissionEnum.CLOSE_ROUNDS,
            enums.PermissionEnum.CREATE_CALL_PERMISSION,
            enums.PermissionEnum.LIST_PROPOSALS,
            enums.PermissionEnum.LIST_CALLS,
            enums.PermissionEnum.LIST_ROUNDS,
            enums.PermissionEnum.MANAGE_PROPOSAL_REVIEW,
            enums.PermissionEnum.UPDATE_CALL,
        ):
            CallRole.MANAGER.add_permission(perm)
            self.call_organizer_role.add_permission(perm)

        CallRole.REVIEWER.add_permission(enums.PermissionEnum.LIST_PROPOSALS)
        CallRole.REVIEWER.add_permission(enums.PermissionEnum.LIST_CALLS)
        self.call_organizer_role.add_permission(enums.PermissionEnum.CREATE_CALL)

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
            state=CallStates.ACTIVE,
            created_by=self.call_organizer_user,
        )

    @property
    def call_organizer_role(self):
        return Role.objects.get_system_role(
            "CUSTOMER.CALL_ORGANIZER",
            content_type=ContentType.objects.get_for_model(
                proposal_models.CallManagingOrganisation
            ),
        )

    @property
    def call_organizer_user(self):
        user = structure_factories.UserFactory()
        self.manager.add_user(user, self.call_organizer_role)
        self.customer.add_user(user, self.call_organizer_role)
        return user

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
            state=RequestedOfferingStates.REQUESTED,
            created_by=self.owner,
            offering=self.offering,
        )

    @cached_property
    def requested_offering_accepted(self):
        return proposal_factories.RequestedOfferingFactory(
            call=self.call,
            state=RequestedOfferingStates.ACCEPTED,
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
            start_time=timezone.now(),
            cutoff_time=timezone.now() + datetime.timedelta(days=10),
        )

    @cached_property
    def new_round(self):
        return proposal_factories.RoundFactory(
            call=self.call,
            start_time=timezone.now(),
            cutoff_time=timezone.now() + datetime.timedelta(days=10),
        )

    @cached_property
    def proposal(self):
        return proposal_factories.ProposalFactory(
            name="New Proposal",
            round=self.round,
            project=self.proposal_project,
        )

    @cached_property
    def proposal_project(self):
        return structure_factories.ProjectFactory(customer=self.customer)

    @cached_property
    def proposal_submitted(self):
        return proposal_factories.ProposalFactory(
            name="Proposal submitted",
            round=self.round,
            state=ProposalStates.SUBMITTED,
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
        self.call.add_user(user, role)
        return user

    @cached_property
    def reviewer_2(self):
        user = structure_factories.UserFactory()
        role = CallRole.REVIEWER
        self.call.add_user(user, role)
        return user

    @cached_property
    def panel_member(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.PANEL_MEMBER)
        return user

    @cached_property
    def call_manager(self):
        user = structure_factories.UserFactory()
        role = CallRole.MANAGER
        self.call.add_user(user, role)
        return user
