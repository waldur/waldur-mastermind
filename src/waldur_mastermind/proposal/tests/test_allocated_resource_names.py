from rest_framework import test

from waldur_core.core.models import NAME_LENGTH
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import utils
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


class AllocatedResourceNameTest(test.APITestCase):
    """Names of the resources a proposal is granted (#274).

    Every resource used to be named after the project, so an allocation with
    several requests produced rows nobody could tell apart, and a long proposal
    name overflowed the resource name column and made acceptance fail.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.project = None
        self.proposal.save()

    def request(self, offering):
        return factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=factories.RequestedOfferingFactory(
                call=self.fixture.call, offering=offering
            ),
            resource=None,
        )

    def allocate(self):
        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)
        self.proposal.refresh_from_db()
        return {
            requested.uuid: requested.resource.name
            for requested in self.proposal.requestedresource_set.all()
        }

    def test_resource_is_named_after_project_and_offering(self):
        requested = self.request(marketplace_factories.OfferingFactory(name="HPC"))

        names = self.allocate()

        self.assertEqual(names[requested.uuid], f"{self.proposal.project.name} - HPC")

    def test_resources_for_different_offerings_are_distinct(self):
        hpc = self.request(marketplace_factories.OfferingFactory(name="HPC"))
        storage = self.request(marketplace_factories.OfferingFactory(name="Storage"))

        names = self.allocate()

        self.assertNotEqual(names[hpc.uuid], names[storage.uuid])

    def test_repeated_offering_is_numbered_in_request_order(self):
        offering = marketplace_factories.OfferingFactory(name="HPC")
        first = self.request(offering)
        second = self.request(offering)

        names = self.allocate()

        project_name = self.proposal.project.name
        self.assertEqual(names[first.uuid], f"{project_name} - HPC (1)")
        self.assertEqual(names[second.uuid], f"{project_name} - HPC (2)")

    def test_long_proposal_name_is_allocated_within_name_limit(self):
        self.proposal.name = "x" * NAME_LENGTH
        self.proposal.save()
        requested = self.request(marketplace_factories.OfferingFactory(name="HPC"))

        names = self.allocate()

        name = names[requested.uuid]
        self.assertEqual(len(name), NAME_LENGTH)
        # The project part gives way, so the offering stays readable.
        self.assertTrue(name.endswith(" - HPC"))
