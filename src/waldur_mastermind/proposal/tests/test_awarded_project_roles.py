"""The team of a project awarded through a call, and who reads a call's results.

An awarded project's principal investigator manages its team, but what the
call awarded -- limits, plan, dates, new orders -- changes only through the
call's own workflow. The awarded-project roles act as project manager,
administrator and member wherever a role is looked up by name, and none of
them holds the permissions that change an allocation. Roles load from the
shipped permissions.yaml, as on a deployment.
"""

import datetime
import io
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole, ProposalRole
from waldur_core.permissions.utils import get_users
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.proposal import rankings
from waldur_mastermind.proposal.enums import ProposalStates, RoundLifecycleStates
from waldur_mastermind.proposal.tests import factories, fixtures

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"


def import_shipped_roles():
    call_command("import_roles", str(PERMISSIONS_YAML), stdout=io.StringIO())


class AwardedProjectTeamTest(test.APITestCase):
    def setUp(self):
        import_shipped_roles()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.end_date = timezone.now().date() + datetime.timedelta(days=30)
        self.resource.save()
        self.pi = structure_factories.UserFactory()
        self.project.add_user(self.pi, ProjectRole.PRINCIPAL_INVESTIGATOR)
        self.collaborator = structure_factories.UserFactory()
        self.project.add_user(self.collaborator, ProjectRole.COLLABORATOR)

    def resource_url(self, action=None):
        return marketplace_factories.ResourceFactory.get_url(self.resource, action)

    def add_user(self, acting_user, role_name):
        self.client.force_authenticate(acting_user)
        return self.client.post(
            structure_factories.ProjectFactory.get_url(self.project, "add_user"),
            {"user": structure_factories.UserFactory().uuid.hex, "role": role_name},
        )

    def test_roles_act_as_their_templates(self):
        self.assertEqual(
            ProjectRole.PRINCIPAL_INVESTIGATOR.template.name, RoleEnum.PROJECT_MANAGER
        )
        self.assertIn(self.pi, get_users(self.project, RoleEnum.PROJECT_MANAGER))
        self.assertIn(
            self.collaborator, get_users(self.project, RoleEnum.PROJECT_MEMBER)
        )

    def test_a_new_call_hands_out_the_awarded_project_roles(self):
        call = factories.CallFactory()
        mapping = {
            row.proposal_role.name: row.project_role.name
            for row in call.proposalprojectrolemapping_set.all()
        }
        self.assertEqual(
            mapping,
            {
                RoleEnum.PROPOSAL_MANAGER: RoleEnum.PROJECT_PRINCIPAL_INVESTIGATOR,
                RoleEnum.PROPOSAL_ADMIN: RoleEnum.PROJECT_CO_INVESTIGATOR,
                RoleEnum.PROPOSAL_MEMBER: RoleEnum.PROJECT_COLLABORATOR,
            },
        )

    def test_the_principal_investigator_does_not_change_the_allocation(self):
        self.client.force_authenticate(self.pi)
        for action, payload in (
            ("update_limits", {"limits": {"cpu": 10}}),
            ("switch_plan", {"plan": self.fixture.plan.uuid.hex}),
            ("terminate", {}),
        ):
            response = self.client.post(self.resource_url(action), payload)
            self.assertEqual(
                response.status_code, status.HTTP_403_FORBIDDEN, (action, response.data)
            )
        response = self.client.patch(
            self.resource_url(), {"end_date": "2099-01-01"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_principal_investigator_does_not_rewrite_what_was_awarded(self):
        """The project's and its resources' names and descriptions were the
        basis of the award."""
        self.client.force_authenticate(self.pi)
        response = self.client.patch(self.resource_url(), {"name": "Renamed"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.patch(
            structure_factories.ProjectFactory.get_url(self.project),
            {"name": "Renamed", "description": "Rewritten"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_principal_investigator_adds_collaborators_but_no_project_manager(self):
        response = self.add_user(self.pi, RoleEnum.PROJECT_COLLABORATOR)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        for role in (RoleEnum.PROJECT_MANAGER, RoleEnum.PROJECT_MEMBER):
            response = self.add_user(self.pi, role)
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, (role, response.data)
            )

    def test_a_project_manager_still_grants_every_project_role(self):
        for role in (
            RoleEnum.PROJECT_MANAGER,
            RoleEnum.PROJECT_ADMIN,
            RoleEnum.PROJECT_MEMBER,
            RoleEnum.PROJECT_PRINCIPAL_INVESTIGATOR,
        ):
            response = self.add_user(self.fixture.manager, role)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, (role, response.data)
            )

    def test_the_awarded_roles_are_not_offered_change_requests(self):
        self.fixture.offering.plugin_options = {
            "enable_resource_limit_change_requests": True,
            "enable_resource_end_date_change_requests": True,
        }
        self.fixture.offering.save()
        for user in (self.pi, self.collaborator):
            self.client.force_authenticate(user)
            response = self.client.post(
                reverse("marketplace-resource-limit-change-request-list"),
                {"resource": self.resource.uuid.hex, "requested_limits": {"cpu": 5}},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
            response = self.client.post(
                reverse("marketplace-resource-end-date-change-request-list"),
                {
                    "resource": self.resource.uuid.hex,
                    "requested_end_date": "2099-01-01",
                },
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # A project member still asks.
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(
            reverse("marketplace-resource-limit-change-request-list"),
            {"resource": self.resource.uuid.hex, "requested_limits": {"cpu": 5}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_the_principal_investigator_does_not_ask_for_a_later_project_end(self):
        self.client.force_authenticate(self.pi)
        response = self.client.post(
            reverse("project-end-date-change-request-list"),
            {
                "project": structure_factories.ProjectFactory.get_url(self.project),
                "requested_end_date": "2099-01-01",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ResourceEndDateTest(test.APITestCase):
    """Editing a resource does not reach its end date."""

    def setUp(self):
        import_shipped_roles()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()
        self.url = marketplace_factories.ResourceFactory.get_url(self.resource)

    def test_a_project_manager_does_not_set_the_end_date_through_an_edit(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.patch(self.url, {"end_date": "2099-01-01"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.patch(self.url, {"name": "Renamed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_owner_does(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(self.url, {"end_date": "2099-01-01"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ProjectEndDateTest(test.APITestCase):
    """A request is approved by whoever sets the date directly."""

    def setUp(self):
        import_shipped_roles()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(
            reverse("project-end-date-change-request-list"),
            {
                "project": structure_factories.ProjectFactory.get_url(self.project),
                "requested_end_date": "2099-01-01",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.approve_url = (
            reverse(
                "project-end-date-change-request-detail",
                kwargs={"uuid": response.data["uuid"]},
            )
            + "approve/"
        )

    def test_a_project_manager_does_not_approve(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_owner_approves(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.approve_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project.refresh_from_db()
        self.assertEqual(self.project.end_date, datetime.date(2099, 1, 1))


class PullPermissionTest(test.APITestCase):
    def setUp(self):
        import_shipped_roles()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        resource = self.fixture.resource
        resource.state = marketplace_models.Resource.States.OK
        resource.backend_id = "backend"
        resource.save()
        self.url = marketplace_factories.ResourceFactory.get_url(resource, "pull")

    def test_a_project_member_does_not_pull(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_project_manager_does(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(self.url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CallResultsReaderTest(test.APITestCase):
    """An organisation reader of the managing organisation reads a round's
    submitted proposals and ranking once its results are out, and not
    before."""

    def setUp(self):
        import_shipped_roles()
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        self.round = self.fixture.round
        self.reader = structure_factories.UserFactory()
        self.fixture.manager.customer.add_user(self.reader, CustomerRole.READER)
        self.client.force_authenticate(self.reader)
        self.url = factories.ProposalFactory.get_url(self.proposal)

    def publish(self):
        self.round.lifecycle_state = RoundLifecycleStates.RESULTS_PUBLISHED
        self.round.save(update_fields=["lifecycle_state"])

    def test_nothing_before_the_results_are_out(self):
        self.round.lifecycle_state = RoundLifecycleStates.DECIDING
        self.round.save(update_fields=["lifecycle_state"])
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertFalse(rankings.can_read(self.reader, self.round))

    def test_the_proposals_and_ranking_once_they_are(self):
        self.publish()
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)
        self.assertTrue(rankings.can_read(self.reader, self.round))

    def test_never_a_draft(self):
        self.publish()
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND
        )


class AwardedProposalFieldTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        # The fixture points other proposals at the same project: the
        # accepted one is the one it was awarded for.
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        self.project = self.fixture.proposal_project
        self.client.force_authenticate(self.fixture.staff)

    def test_a_project_names_the_proposal_it_was_awarded_for(self):
        expected = {
            "proposal_uuid": self.proposal.uuid.hex,
            "proposal_name": self.proposal.name,
            "call_uuid": self.proposal.round.call.uuid.hex,
            "call_name": self.proposal.round.call.name,
            "round_uuid": self.proposal.round.uuid.hex,
            "round_name": self.proposal.round.name,
            "can_read": True,
        }
        response = self.client.get(
            structure_factories.ProjectFactory.get_url(self.project)
        )
        self.assertEqual(response.data["awarded_proposal"], expected)
        response = self.client.get(structure_factories.ProjectFactory.get_list_url())
        row = next(row for row in response.data if row["uuid"] == self.project.uuid.hex)
        self.assertEqual(row["awarded_proposal"], expected)

    def test_a_collaborator_added_later_sees_only_where_it_came_from(self):
        """The proposal's team opens the proposal from its project; someone
        added to the project after the award reads its name, not its content."""
        applicant = structure_factories.UserFactory()
        self.proposal.add_user(applicant, ProposalRole.MANAGER)
        later = structure_factories.UserFactory()
        for user in (applicant, later):
            self.project.add_user(user, ProjectRole.COLLABORATOR)

        for user, can_read in ((applicant, True), (later, False)):
            self.client.force_authenticate(user)
            response = self.client.get(
                structure_factories.ProjectFactory.get_url(self.project)
            )
            awarded = response.data["awarded_proposal"]
            self.assertEqual(awarded["proposal_name"], self.proposal.name)
            self.assertEqual(awarded["can_read"], can_read)
            self.assertEqual(
                self.client.get(
                    factories.ProposalFactory.get_url(self.proposal)
                ).status_code,
                status.HTTP_200_OK if can_read else status.HTTP_404_NOT_FOUND,
            )

    def test_any_other_project_names_none(self):
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        response = self.client.get(structure_factories.ProjectFactory.get_url(project))
        self.assertIsNone(response.data["awarded_proposal"])
