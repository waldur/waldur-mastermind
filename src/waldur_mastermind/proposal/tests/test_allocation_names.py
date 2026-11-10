import datetime

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.fixtures import CallRole
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import allocation_names, utils
from waldur_mastermind.proposal.enums import CallStates, ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


class AllocationNamingBase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.slug = "store"
        self.call.backend_id = ""
        self.call.save()
        self.round = self.fixture.round
        self.proposal = self.fixture.proposal
        self.proposal.name = "Clinical school"
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.project = None
        self.proposal.save()

    def request(self, offering):
        return factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=factories.RequestedOfferingFactory(
                call=self.call, offering=offering
            ),
            resource=None,
        )

    def allocate(self):
        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)
        self.proposal.refresh_from_db()
        return self.proposal.project

    def resource_names(self):
        return {
            requested.uuid: requested.resource.name
            for requested in self.proposal.requestedresource_set.all()
        }


class AllocatedProjectNameTest(AllocationNamingBase):
    """The project an accepted proposal creates is named by the call's
    project_name_template (#1162)."""

    def test_call_without_template_keeps_the_old_name(self):
        project = self.allocate()

        round_start = self.round.start_time.strftime("%Y-%m-%d")
        self.assertEqual(project.name, f"store - {round_start} - Clinical school")

    def test_reference_code_prefixes_the_old_name(self):
        self.call.backend_id = "STORE-1"
        self.call.save()

        project = self.allocate()

        self.assertTrue(project.name.startswith("STORE-1 - "))

    def test_template_with_proposal_name_only(self):
        self.call.project_name_template = "{proposal_name}"
        self.call.save()

        project = self.allocate()

        self.assertEqual(project.name, "Clinical school")

    def test_template_combines_placeholders(self):
        self.call.project_name_template = "{call_slug}/{org_slug}: {proposal_name}"
        self.call.save()

        project = self.allocate()

        org_slug = self.call.manager.customer.slug
        self.assertEqual(project.name, f"store/{org_slug}: Clinical school")

    def test_braces_in_proposal_name_are_kept_literally(self):
        self.call.project_name_template = "{proposal_name}"
        self.call.save()
        self.proposal.name = "Study {round_start} {0} {__class__}"
        self.proposal.save()

        project = self.allocate()

        self.assertEqual(project.name, "Study {round_start} {0} {__class__}")

    def test_template_rendering_to_nothing_falls_back_to_the_old_name(self):
        call = self.proposal.round.call
        call.project_name_template = "  {call_slug}  "
        call.slug = ""

        name = allocation_names.allocated_project_name(
            self.proposal, datetime.date(2026, 11, 10)
        )

        self.assertTrue(name.endswith(" - Clinical school"))

    def test_invalid_stored_template_falls_back_to_the_old_name(self):
        # Written past the API, e.g. by an older import.
        self.call.project_name_template = "{nope}"
        self.call.save()

        project = self.allocate()

        self.assertTrue(project.name.endswith(" - Clinical school"))


class AllocatedResourceNamePatternTest(AllocationNamingBase):
    """A resource allocation orders is named by its offering's
    resource_name_pattern, as an ordinary order is (#1162)."""

    def setUp(self):
        super().setUp()
        self.call.project_name_template = "{proposal_name}"
        self.call.save()

    def test_offering_pattern_names_the_resource(self):
        offering = marketplace_factories.OfferingFactory(
            name="Cold Storage",
            plugin_options={"resource_name_pattern": "store-{project_name}"},
        )
        requested = self.request(offering)

        self.allocate()

        self.assertEqual(self.resource_names()[requested.uuid], "store-clinical-school")

    def test_repeated_offering_is_numbered_by_the_pattern(self):
        offering = marketplace_factories.OfferingFactory(
            name="Cold Storage",
            plugin_options={"resource_name_pattern": "store-{project_name}"},
        )
        first = self.request(offering)
        second = self.request(offering)

        self.allocate()

        names = self.resource_names()
        self.assertEqual(
            {names[first.uuid], names[second.uuid]},
            {"store-clinical-school", "store-clinical-school-2"},
        )

    def test_pattern_with_counter_numbers_itself(self):
        offering = marketplace_factories.OfferingFactory(
            name="Cold Storage",
            plugin_options={"resource_name_pattern": "store-{project_name}-{counter}"},
        )
        first = self.request(offering)
        second = self.request(offering)

        self.allocate()

        names = self.resource_names()
        self.assertEqual(
            {names[first.uuid], names[second.uuid]},
            {"store-clinical-school", "store-clinical-school-2"},
        )

    def test_offering_without_pattern_keeps_project_and_offering_name(self):
        requested = self.request(marketplace_factories.OfferingFactory(name="HPC"))

        self.allocate()

        self.assertEqual(self.resource_names()[requested.uuid], "Clinical school - HPC")

    def test_broken_pattern_falls_back_to_project_and_offering_name(self):
        offering = marketplace_factories.OfferingFactory(
            name="HPC", plugin_options={"resource_name_pattern": "{project_name"}
        )
        requested = self.request(offering)

        self.allocate()

        self.assertEqual(self.resource_names()[requested.uuid], "Clinical school - HPC")


class AwardItemIndexTest(AllocationNamingBase):
    """An item ordered again keeps the number allocation gave it."""

    def test_repeated_items_are_numbered_in_award_order(self):
        offering = marketplace_factories.OfferingFactory(name="HPC")
        self.request(offering)
        self.request(offering)
        self.allocate()

        items = list(
            self.proposal.awarded_resources.filter(
                requested_offering__offering=offering
            ).order_by("created", "id")
        )

        self.assertEqual([utils.award_item_index(item) for item in items], [1, 2])

    def test_single_item_has_no_number(self):
        offering = marketplace_factories.OfferingFactory(name="HPC")
        self.request(offering)
        self.allocate()

        item = self.proposal.awarded_resources.get(
            requested_offering__offering=offering
        )

        self.assertIsNone(utils.award_item_index(item))


@ddt
class CallProjectNameTemplateSerializerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = factories.CallFactory(
            manager=self.fixture.manager,
            state=CallStates.ACTIVE,
            created_by=self.fixture.owner,
        )
        self.call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        self.url = factories.CallFactory.get_protected_url(self.call)
        self.client.force_authenticate(self.fixture.call_manager)

    def patch(self, value):
        return self.client.patch(self.url, {"project_name_template": value})

    @data(
        "{proposal_name}",
        "{call_prefix} - {round_start} - {proposal_name}",
        "{call_slug}-{proposal_slug} ({year}) {call_name} {round_slug} {org_slug}",
    )
    def test_valid_template_is_saved(self, template):
        response = self.patch(template)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertEqual(self.call.project_name_template, template)

    @data("", "   ", None)
    def test_empty_template_means_the_default(self, value):
        self.call.project_name_template = "{proposal_name}"
        self.call.save()

        response = self.patch(value)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertIsNone(self.call.project_name_template)

    @data(
        "{unknown}",
        "{}",
        "{0}",
        "{proposal_name.__class__}",
        "{proposal_name[0]}",
        "{proposal_name!r}",
        "{proposal_name:>40}",
        "{proposal_name",
    )
    def test_invalid_template_is_refused(self, template):
        response = self.patch(template)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_name_template", response.data)

    def test_unknown_placeholder_error_lists_the_allowed_ones(self):
        response = self.patch("{unknown}")

        message = str(response.data["project_name_template"])
        self.assertIn("{unknown}", message)
        self.assertIn("{proposal_name}", message)

    @override_config(PROJECT_NAME_REGEX=r"^[A-Za-z0-9 ]+$")
    def test_literal_text_is_held_to_the_project_name_rule(self):
        response = self.patch("{proposal_name} / archive")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_is_allowed_once_proposals_exist(self):
        factories.ProposalFactory(round=factories.RoundFactory(call=self.call))

        response = self.patch("{proposal_name}")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_public_call_shows_the_template(self):
        self.call.project_name_template = "{proposal_name}"
        self.call.save()

        response = self.client.get(factories.CallFactory.get_public_url(self.call))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["project_name_template"], "{proposal_name}")

    def test_public_call_cannot_write_the_template(self):
        response = self.client.patch(
            factories.CallFactory.get_public_url(self.call),
            {"project_name_template": "{proposal_name}"},
        )

        self.call.refresh_from_db()
        self.assertIsNone(self.call.project_name_template)
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)
