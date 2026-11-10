from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProjectRole, ProposalRole
from waldur_core.structure import utils as structure_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import BASIC_OFFERING, BillingTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal import utils as proposal_utils
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories as proposal_factories
from waldur_mastermind.proposal.tests import fixtures
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests.helpers import override_openstack_settings

FINANCIAL_FIELDS = ("billing_price_estimate", "project_credit")


def award(proposal, resource):
    """Record ``resource`` as provisioned for an awarded item of
    ``proposal``, as allocation does."""
    return proposal_models.AwardedResource.objects.create(
        proposal=proposal,
        requested_offering=proposal_factories.RequestedOfferingFactory(
            call=proposal.round.call, offering=resource.offering
        ),
        resource=resource,
    )


# Backend details of a resource a member reads but a call manager does not.
BACKEND_FIELDS = ("report", "backend_metadata", "options", "attributes", "endpoints")
TENANT_CREDENTIAL_FIELDS = ("user_username", "user_password", "access_url")


class CallManagerProjectAccessTest(test.APITestCase):
    """A call manager reads, and only reads, the projects their call's
    accepted proposals were allocated, and those projects' resources."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call_manager = self.fixture.call_manager
        self.project = self.make_accepted_project()
        self.resource = marketplace_factories.ResourceFactory(
            project=self.project, offering=self.fixture.offering
        )
        award(self.proposal, self.resource)

    def make_accepted_project(self, round=None):
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        self.proposal = proposal_factories.ProposalFactory(
            round=round or self.fixture.round,
            state=ProposalStates.ACCEPTED,
            project=project,
        )
        return project

    def project_url(self, project=None, action=None):
        return structure_factories.ProjectFactory.get_url(
            project or self.project, action=action
        )

    def resource_url(self, action=None):
        return marketplace_factories.ResourceFactory.get_url(
            self.resource, action=action
        )

    def list_project_uuids(self):
        response = self.client.get(structure_factories.ProjectFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    def list_resource_uuids(self):
        response = self.client.get(marketplace_factories.ResourceFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    # Reading

    def test_call_manager_reads_the_order_period_usage(self):
        # It sums the usage behind limit_usage, which retrieve shows them too.
        self.fixture.offering.plugin_options = {"show_order_period_totals": True}
        self.fixture.offering.save(update_fields=["plugin_options"])
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(self.resource_url(action="order_period_usage"))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_call_manager_retrieves_and_lists_the_project(self):
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(self.project_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.project.uuid.hex)
        self.assertIn(self.project.uuid.hex, self.list_project_uuids())

    def test_call_manager_retrieves_and_lists_the_resource(self):
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(self.resource_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.resource.uuid.hex)
        self.assertIn(self.resource.uuid.hex, self.list_resource_uuids())

    def test_call_manager_reads_the_project_usage_stats(self):
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(self.project_url(action="stats"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("components", response.data)

    def test_call_manager_reads_the_runtime_states_of_the_project(self):
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(
            "/api/marketplace-runtime-states/",
            {"project_uuid": self.project.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_call_manager_reads_the_component_usages_of_the_resource(self):
        usage = marketplace_factories.ComponentUsageFactory(resource=self.resource)
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(
            marketplace_factories.ComponentUsageFactory.get_list_url(),
            {"resource_uuid": self.resource.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row["uuid"] for row in response.data], [usage.uuid.hex])

    def test_call_manager_on_the_applicant_team_gets_nothing_through_the_call(self):
        # On the applicant team, the call manager is the applicant: the
        # project is theirs through the team's own roles, never the call's.
        self.proposal.add_user(self.call_manager, ProposalRole.MEMBER)
        self.client.force_authenticate(self.call_manager)

        self.assertNotIn(self.project.uuid.hex, self.list_project_uuids())
        self.assertNotIn(self.resource.uuid.hex, self.list_resource_uuids())

    def test_project_of_a_proposal_that_is_not_accepted_stays_hidden(self):
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        proposal_factories.ProposalFactory(
            round=self.fixture.round,
            state=ProposalStates.SUBMITTED,
            project=project,
        )
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(self.project_url(project))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unrelated_project_of_the_organisation_stays_hidden(self):
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        self.client.force_authenticate(self.call_manager)

        self.assertEqual(
            self.client.get(self.project_url(project)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertNotIn(project.uuid.hex, self.list_project_uuids())

    # Financial data

    def test_financial_fields_are_withheld_from_the_call_manager(self):
        self.client.force_authenticate(self.call_manager)

        detail = self.client.get(self.project_url()).data
        row = next(
            row
            for row in self.client.get(
                structure_factories.ProjectFactory.get_list_url()
            ).data
            if row["uuid"] == self.project.uuid.hex
        )

        for field in FINANCIAL_FIELDS:
            self.assertNotIn(field, detail)
            self.assertNotIn(field, row)

    def test_organisation_owner_still_gets_financial_fields(self):
        self.client.force_authenticate(self.fixture.owner)

        detail = self.client.get(self.project_url()).data

        for field in FINANCIAL_FIELDS:
            self.assertIn(field, detail)

    def test_call_manager_with_a_project_role_gets_financial_fields(self):
        self.project.add_user(self.call_manager, ProjectRole.MANAGER)
        self.client.force_authenticate(self.call_manager)

        detail = self.client.get(self.project_url()).data

        for field in FINANCIAL_FIELDS:
            self.assertIn(field, detail)

    def test_invoices_and_event_feed_stay_closed(self):
        self.client.force_authenticate(self.call_manager)

        invoices = self.client.get(
            "/api/invoices/", {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        events = self.client.get("/api/events/", {"scope": self.project_url()})

        self.assertEqual(invoices.status_code, status.HTTP_200_OK)
        self.assertEqual(invoices.data, [])
        # The project does not resolve as an event scope for this user.
        self.assertEqual(events.status_code, status.HTTP_400_BAD_REQUEST)

    # Writing

    def test_call_manager_cannot_update_or_delete_the_project(self):
        self.client.force_authenticate(self.call_manager)

        patch = self.client.patch(self.project_url(), {"name": "Renamed"})
        delete = self.client.delete(self.project_url())

        self.assertEqual(patch.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(delete.status_code, status.HTTP_404_NOT_FOUND)
        self.project.refresh_from_db()
        self.assertNotEqual(self.project.name, "Renamed")
        self.assertFalse(self.project.is_removed)

    def test_call_manager_cannot_list_or_manage_the_project_team(self):
        user = structure_factories.UserFactory()
        self.client.force_authenticate(self.call_manager)

        listing = self.client.get(self.project_url(action="list_users"))
        adding = self.client.post(
            self.project_url(action="add_user"),
            {"user": user.uuid.hex, "role": "PROJECT.MEMBER"},
        )

        self.assertEqual(listing.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(adding.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(self.project.has_user(user))

    def test_call_manager_cannot_act_on_the_resource(self):
        self.client.force_authenticate(self.call_manager)

        patch = self.client.patch(self.resource_url(), {"name": "Renamed"})
        terminate = self.client.post(self.resource_url(action="terminate"))
        team = self.client.get(self.resource_url(action="team"))

        self.assertEqual(patch.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(terminate.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(team.status_code, status.HTTP_404_NOT_FOUND)
        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.name, "Renamed")

    def test_call_manager_cannot_create_an_order_in_the_project(self):
        plan = marketplace_factories.PlanFactory(offering=self.fixture.offering)
        self.client.force_authenticate(self.call_manager)

        response = self.client.post(
            marketplace_factories.OrderFactory.get_list_url(),
            {
                "project": self.project_url(),
                "offering": marketplace_factories.OfferingFactory.get_public_url(
                    self.fixture.offering
                ),
                "plan": marketplace_factories.PlanFactory.get_public_url(plan),
                "attributes": {},
            },
            format="json",
        )

        # The project is not among those the order form accepts.
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project", response.data)
        self.assertFalse(
            marketplace_models.Order.objects.filter(project=self.project).exists()
        )

    # Who else

    def test_call_manager_of_another_call_sees_nothing(self):
        other_manager = structure_factories.UserFactory()
        self.fixture.new_call.add_user(other_manager, CallRole.MANAGER)
        self.assert_sees_nothing(other_manager)

    def test_reviewer_sees_nothing(self):
        self.assert_sees_nothing(self.fixture.reviewer_1)

    def test_panel_member_sees_nothing(self):
        self.assert_sees_nothing(self.fixture.panel_member)

    def test_revoking_the_call_role_removes_access(self):
        self.call.remove_user(self.call_manager)
        self.assert_sees_nothing(self.call_manager)

    def assert_sees_nothing(self, user):
        marketplace_factories.ComponentUsageFactory(resource=self.resource)
        self.client.force_authenticate(user)
        usages = self.client.get(
            marketplace_factories.ComponentUsageFactory.get_list_url(),
            {"resource_uuid": self.resource.uuid.hex},
        )
        self.assertEqual(usages.data, [])
        self.assertEqual(
            self.client.get(self.project_url()).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(self.resource_url()).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertNotIn(self.project.uuid.hex, self.list_project_uuids())
        self.assertNotIn(self.resource.uuid.hex, self.list_resource_uuids())

    # Cost

    def test_project_list_query_count_does_not_grow_with_projects(self):
        self.client.force_authenticate(self.call_manager)
        url = structure_factories.ProjectFactory.get_list_url()

        with CaptureQueriesContext(connection) as one:
            self.assertEqual(len(self.client.get(url).data), 1)

        for _ in range(3):
            self.make_accepted_project()

        with CaptureQueriesContext(connection) as four:
            self.assertEqual(len(self.client.get(url).data), 4)

        self.assertEqual(len(one), len(four))


class CallManagerResourceDetailsTest(test.APITestCase):
    """What a call manager reads of an allocated resource stops short of its
    backend: no plugin details, no offering endpoint, no report, metadata,
    options or attributes, and no order with its cost."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call_manager = self.fixture.call_manager
        self.project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        proposal = proposal_factories.ProposalFactory(
            round=self.fixture.round,
            state=ProposalStates.ACCEPTED,
            project=self.project,
        )
        self.member = structure_factories.UserFactory()
        ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_RESOURCES)
        self.project.add_user(self.member, ProjectRole.MEMBER)
        self.tenant = openstack_factories.TenantFactory(project=self.project)
        self.resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.fixture.offering,
            scope=self.tenant,
            report=[{"header": "Access", "body": "ssh root@host password"}],
            backend_metadata={"state": "OK", "internal_ip": "10.0.0.5"},
            options={"secret": "value"},
            attributes={"user_data": "#cloud-config"},
        )
        award(proposal, self.resource)
        self.order = marketplace_factories.OrderFactory(
            resource=self.resource,
            project=self.project,
            offering=self.fixture.offering,
        )

    def resource_url(self, action=None):
        return marketplace_factories.ResourceFactory.get_url(
            self.resource, action=action
        )

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=True)
    def test_details_are_refused_to_the_call_manager(self):
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(self.resource_url(action="details"))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        for field in TENANT_CREDENTIAL_FIELDS:
            self.assertNotIn(field, response.data or {})

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=True)
    def test_project_member_still_reads_details_with_credentials(self):
        self.client.force_authenticate(self.member)

        response = self.client.get(self.resource_url(action="details"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in TENANT_CREDENTIAL_FIELDS:
            self.assertIn(field, response.data)

    def test_offering_endpoint_is_refused_to_the_call_manager(self):
        self.client.force_authenticate(self.call_manager)

        response = self.client.get(self.resource_url(action="offering"))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_backend_fields_are_withheld_from_the_call_manager(self):
        self.client.force_authenticate(self.call_manager)

        detail = self.client.get(self.resource_url())
        listing = self.client.get(marketplace_factories.ResourceFactory.get_list_url())

        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        [row] = [r for r in listing.data if r["uuid"] == self.resource.uuid.hex]
        for data in (detail.data, row):
            for field in BACKEND_FIELDS + ("error_traceback",):
                self.assertNotIn(field, data)
            self.assertIsNone(data["creation_order"])
            self.assertIsNone(data["order_in_progress"])

    def test_project_member_still_reads_backend_fields(self):
        self.client.force_authenticate(self.member)

        detail = self.client.get(self.resource_url())
        listing = self.client.get(marketplace_factories.ResourceFactory.get_list_url())

        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        [row] = [r for r in listing.data if r["uuid"] == self.resource.uuid.hex]
        for data in (detail.data, row):
            for field in BACKEND_FIELDS:
                self.assertIn(field, data)
            self.assertEqual(data["report"], self.resource.report)
            self.assertEqual(data["creation_order"]["uuid"], self.order.uuid.hex)

    def test_call_manager_with_a_project_role_reads_backend_fields(self):
        self.project.add_user(self.call_manager, ProjectRole.MEMBER)
        self.client.force_authenticate(self.call_manager)

        detail = self.client.get(self.resource_url())

        for field in BACKEND_FIELDS:
            self.assertIn(field, detail.data)
        self.assertEqual(detail.data["creation_order"]["uuid"], self.order.uuid.hex)


class CallManagerProjectCostOrderingTest(test.APITestCase):
    """Sorting projects by estimated cost does not rank the allocated projects
    whose cost the call manager is not shown."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call_manager = self.fixture.call_manager
        self.cheap = self.make_project(10, accepted=True)
        self.dear = self.make_project(1000, accepted=True)
        # A project the call manager is a member of keeps its real cost.
        self.own = self.make_project(100, accepted=False)
        self.own.add_user(self.call_manager, ProjectRole.MEMBER)

    def make_project(self, cost, accepted):
        project = structure_factories.ProjectFactory(customer=self.fixture.customer)
        if accepted:
            proposal_factories.ProposalFactory(
                round=self.fixture.round,
                state=ProposalStates.ACCEPTED,
                project=project,
            )
        billing_models.PriceEstimate.objects.update_or_create(
            content_type=ContentType.objects.get_for_model(project),
            object_id=project.id,
            defaults={"total": cost},
        )
        return project

    def ordered_uuids(self, ordering):
        response = self.client.get(
            structure_factories.ProjectFactory.get_list_url(), {"o": ordering}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["uuid"] for row in response.data]

    def test_granted_projects_sort_as_if_they_had_no_cost(self):
        self.client.force_authenticate(self.call_manager)

        for ordering in ("-estimated_cost", "estimated_cost"):
            uuids = self.ordered_uuids(ordering)
            self.assertEqual(len(uuids), 3)
            # The granted projects sort together, as projects without a cost,
            # instead of either side of the one whose cost is shown (10 and
            # 1000 around 100).
            self.assertIn(uuids.index(self.own.uuid.hex), (0, 2))

    def test_owner_sorts_by_the_real_cost(self):
        self.client.force_authenticate(self.fixture.owner)

        descending = self.ordered_uuids("-estimated_cost")

        expected = [self.dear.uuid.hex, self.own.uuid.hex, self.cheap.uuid.hex]
        self.assertEqual([uuid for uuid in descending if uuid in expected], expected)


class CallManagerAllocationIsolationTest(test.APITestCase):
    """In an allocated project the call manager reads only the resources the
    allocation provisioned: a resource ordered into the project by other means
    is invisible, and the project's own figures count only the allocated ones.
    Project members keep seeing everything."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call_manager = self.fixture.call_manager
        self.project = structure_factories.ProjectFactory(
            customer=self.fixture.customer
        )
        self.proposal = proposal_factories.ProposalFactory(
            round=self.fixture.round,
            state=ProposalStates.ACCEPTED,
            project=self.project,
        )
        self.allocated = self.make_resource("cpu", "ACTIVE")
        award(self.proposal, self.allocated)
        # Ordered into the project later, outside the call.
        self.outside = self.make_resource("gpu", "SHUTOFF")
        self.member = structure_factories.UserFactory()
        ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_RESOURCES)
        self.project.add_user(self.member, ProjectRole.MEMBER)

    def make_resource(self, component_type, runtime_state):
        offering = marketplace_factories.OfferingFactory()
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type=component_type,
            billing_type=BillingTypes.LIMIT,
        )
        resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=offering,
            state=marketplace_models.Resource.States.OK,
            name=f"{component_type}-resource",
            backend_id=f"{component_type}-backend",
            limits={component_type: 5},
            backend_metadata={"runtime_state": runtime_state},
        )
        resource.usage = marketplace_factories.ComponentUsageFactory(
            resource=resource, component=component
        )
        return resource

    def resource_url(self, resource):
        return marketplace_factories.ResourceFactory.get_url(resource)

    def project_url(self, action=None):
        return structure_factories.ProjectFactory.get_url(self.project, action=action)

    def resource_uuids(self, **params):
        response = self.client.get(
            marketplace_factories.ResourceFactory.get_list_url(), params
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    def usage_uuids(self, **params):
        response = self.client.get(
            marketplace_factories.ComponentUsageFactory.get_list_url(), params
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    def runtime_states(self, **params):
        response = self.client.get("/api/marketplace-runtime-states/", params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["value"] for row in response.data]

    def stats_offerings(self, **params):
        response = self.client.get(self.project_url(action="stats"), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["offering_uuid"] for row in response.data["components"]}

    def project_row(self, **params):
        response = self.client.get(
            structure_factories.ProjectFactory.get_list_url(), params
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = [row for row in response.data if row["uuid"] == self.project.uuid.hex]
        return rows[0] if rows else None

    # The call manager

    def test_resource_outside_the_call_is_not_retrievable(self):
        self.client.force_authenticate(self.call_manager)

        self.assertEqual(
            self.client.get(self.resource_url(self.allocated)).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.get(self.resource_url(self.outside)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_resource_outside_the_call_is_not_listed(self):
        self.client.force_authenticate(self.call_manager)

        expected = {self.allocated.uuid.hex}
        self.assertEqual(self.resource_uuids(), expected)
        self.assertEqual(
            self.resource_uuids(project_uuid=self.project.uuid.hex), expected
        )

    def test_usages_of_the_resource_outside_the_call_are_hidden(self):
        self.client.force_authenticate(self.call_manager)

        self.assertEqual(self.usage_uuids(), {self.allocated.usage.uuid.hex})
        self.assertEqual(self.usage_uuids(resource_uuid=self.outside.uuid.hex), set())
        response = self.client.get(
            f"{marketplace_factories.ComponentUsageFactory.get_list_url()}"
            f"{self.outside.usage.uuid.hex}/"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_runtime_states_cover_only_the_allocated_resources(self):
        self.client.force_authenticate(self.call_manager)

        self.assertEqual(
            self.runtime_states(project_uuid=self.project.uuid.hex), ["ACTIVE"]
        )
        self.assertEqual(
            self.runtime_states(
                project_uuid=self.project.uuid.hex,
                offering_uuid=self.outside.offering.uuid.hex,
            ),
            [],
        )

    def test_usage_stats_cover_only_the_allocated_resources(self):
        self.client.force_authenticate(self.call_manager)

        expected = {self.allocated.offering.uuid.hex}
        self.assertEqual(self.stats_offerings(), expected)
        self.assertEqual(self.stats_offerings(for_current_month=True), expected)

    def test_project_counts_cover_only_the_allocated_resources(self):
        self.client.force_authenticate(self.call_manager)

        detail = self.client.get(self.project_url()).data
        row = self.project_row()

        category = str(self.allocated.offering.category.uuid)
        for data in (detail, row):
            self.assertEqual(data["resources_count"], 1)
            self.assertEqual(data["marketplace_resource_count"], {category: 1})
            self.assertFalse(data["has_metrics"])

    def test_project_search_does_not_match_the_resource_outside_the_call(self):
        self.client.force_authenticate(self.call_manager)

        self.assertIsNotNone(self.project_row(query=self.allocated.name))
        self.assertIsNone(self.project_row(query=self.outside.name))
        self.assertIsNone(self.project_row(query=self.outside.backend_id))

    # Project members

    def test_project_member_still_sees_every_resource(self):
        self.client.force_authenticate(self.member)
        everything = {self.allocated.uuid.hex, self.outside.uuid.hex}

        self.assertEqual(
            self.resource_uuids(project_uuid=self.project.uuid.hex), everything
        )
        self.assertEqual(
            self.client.get(self.resource_url(self.outside)).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.usage_uuids(),
            {self.allocated.usage.uuid.hex, self.outside.usage.uuid.hex},
        )
        self.assertEqual(
            self.runtime_states(project_uuid=self.project.uuid.hex),
            ["ACTIVE", "SHUTOFF"],
        )
        self.assertEqual(
            self.stats_offerings(),
            {self.allocated.offering.uuid.hex, self.outside.offering.uuid.hex},
        )
        self.assertEqual(self.client.get(self.project_url()).data["resources_count"], 2)
        self.assertIsNotNone(self.project_row(query=self.outside.name))

    def test_call_manager_with_a_project_role_sees_every_resource(self):
        self.project.add_user(self.call_manager, ProjectRole.MEMBER)
        self.client.force_authenticate(self.call_manager)

        self.assertEqual(
            self.resource_uuids(project_uuid=self.project.uuid.hex),
            {self.allocated.uuid.hex, self.outside.uuid.hex},
        )
        self.assertEqual(self.client.get(self.project_url()).data["resources_count"], 2)

    # What counts as allocated

    def test_resource_linked_only_through_the_request_is_allocated(self):
        # Proposals allocated before awards existed record the resource on the
        # request alone.
        legacy = self.make_resource("ram", "PAUSED")
        proposal_factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=proposal_factories.RequestedOfferingFactory(
                call=self.fixture.call, offering=legacy.offering
            ),
            resource=legacy,
        )
        self.client.force_authenticate(self.call_manager)

        self.assertEqual(
            self.resource_uuids(), {self.allocated.uuid.hex, legacy.uuid.hex}
        )

    def test_allocated_resource_moved_to_another_project_is_hidden(self):
        other = structure_factories.ProjectFactory(customer=self.fixture.customer)
        self.allocated.project = other
        self.allocated.save(update_fields=["project"])
        self.client.force_authenticate(self.call_manager)

        self.assertEqual(self.resource_uuids(), set())
        self.assertEqual(
            self.client.get(self.resource_url(self.allocated)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_moving_the_project_to_another_organisation_ends_the_grant(self):
        structure_utils.move_project(
            self.project, structure_factories.CustomerFactory()
        )
        self.client.force_authenticate(self.call_manager)

        self.assertEqual(
            self.client.get(self.project_url()).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertIsNone(self.project_row())
        self.assertEqual(
            self.client.get(self.resource_url(self.allocated)).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(self.resource_uuids(), set())
        self.assertEqual(self.usage_uuids(), set())


class CallManagerAllocatedResourcesTest(test.APITestCase):
    """End to end: what ``allocate_proposal`` provisions is what the call
    manager reads; a resource ordered into the project afterwards is not."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.requestedresource_set.all().delete()
        offering = marketplace_factories.OfferingFactory(type=BASIC_OFFERING)
        plan = marketplace_factories.PlanFactory(offering=offering)
        proposal_factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=proposal_factories.RequestedOfferingFactory(
                call=self.fixture.call, offering=offering, plan=plan
            ),
            resource=None,
        )
        self.proposal.project = None
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        proposal_utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)
        self.proposal.refresh_from_db()

    def test_call_manager_reads_what_allocation_provisioned_and_nothing_else(self):
        [allocated] = marketplace_models.Resource.objects.filter(
            project=self.proposal.project
        )
        outside = marketplace_factories.ResourceFactory(project=self.proposal.project)
        self.client.force_authenticate(self.fixture.call_manager)

        response = self.client.get(
            marketplace_factories.ResourceFactory.get_list_url(),
            {"project_uuid": self.proposal.project.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row["uuid"] for row in response.data], [allocated.uuid.hex])
        self.assertEqual(
            self.client.get(
                marketplace_factories.ResourceFactory.get_url(outside)
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
