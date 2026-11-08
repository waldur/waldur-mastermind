from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories


def values(response, name):
    [facet] = [f for f in response.data if f["name"] == name]
    return {v["value"]: v["count"] for v in facet["values"]}


def labels(response, name):
    [facet] = [f for f in response.data if f["name"] == name]
    return {v["value"]: v["label"] for v in facet["values"]}


class ProposalFacetsTest(test.APITestCase):
    def setUp(self):
        self.round = factories.RoundFactory()
        self.other_round = factories.RoundFactory()
        factories.ProposalFactory(
            round=self.round, state=models.ProposalStates.SUBMITTED
        )
        factories.ProposalFactory(round=self.round, state=models.ProposalStates.DRAFT)
        factories.ProposalFactory(
            round=self.other_round, state=models.ProposalStates.SUBMITTED
        )
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))

    def get(self, url_name="proposal-proposal-facets", **params):
        response = self.client.get(reverse(url_name), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def test_states_are_stored_values_with_labels(self):
        response = self.get()

        self.assertEqual(values(response, "state"), {"submitted": 2, "draft": 1})
        self.assertEqual(labels(response, "state")["submitted"], "Submitted")

    def test_call_narrows_states(self):
        response = self.get(call_uuid=self.round.call.uuid.hex)

        self.assertEqual(values(response, "state"), {"submitted": 1, "draft": 1})
        self.assertEqual(
            values(response, "call"),
            {self.round.call.uuid.hex: 2, self.other_round.call.uuid.hex: 1},
        )

    def test_reviews(self):
        proposal = models.Proposal.objects.filter(round=self.round).first()
        factories.ReviewFactory(proposal=proposal)

        response = self.get("proposal-review-facets")

        self.assertEqual(values(response, "proposal"), {proposal.uuid.hex: 1})
