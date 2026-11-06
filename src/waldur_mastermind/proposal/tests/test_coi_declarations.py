"""The wording of the conflict-of-interest declaration a step asks for.

The confirmations themselves -- an evaluator role's, a review's, a delegated
contribution's -- are tested where each is made; here the call team words
the declaration, and the description and a call's export carry it.
"""

from rest_framework import status, test

from waldur_mastermind.proposal import (
    call_transfer,
    coi_declarations,
    workflow_description,
)
from waldur_mastermind.proposal.tests import factories, fixtures


class CoiDeclarationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.step = self.call.workflow_steps.get(key="administrative_check")

    def test_the_call_team_words_it_for_the_call_and_for_a_step(self):
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call),
            {"coi_declaration_text": "No ties to the applicants."},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, self.step),
            {"requires_coi_confirmation": True, "coi_declaration_text": "Own words."},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.step.refresh_from_db()
        self.assertEqual(self.call.coi_declaration_text, "No ties to the applicants.")
        self.assertEqual(coi_declarations.for_step(self.step), "Own words.")

    def test_others_cannot_word_it(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, self.step),
            {"coi_declaration_text": "Mine."},
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.step.refresh_from_db()
        self.assertEqual(self.step.coi_declaration_text, "")

    def test_the_description_says_which_wording_applies(self):
        self.step.requires_coi_confirmation = True
        self.step.save()

        def described():
            description = workflow_description.describe_call(self.call)
            step = next(s for s in description["steps"] if s["key"] == self.step.key)
            return step["coi_declaration"]

        self.assertEqual(
            described(), {"text": "", "source": "platform", "editable": True}
        )
        self.call.coi_declaration_text = "The call's."
        self.call.save()
        self.assertEqual(described()["source"], "call")
        self.step.coi_declaration_text = "The step's."
        self.step.save()
        self.assertEqual(
            described(), {"text": "The step's.", "source": "step", "editable": True}
        )
        self.step.requires_coi_confirmation = False
        self.step.save()
        self.assertIsNone(described())

    def test_the_wording_travels_with_the_call(self):
        self.call.coi_declaration_text = "The call's."
        self.call.save()
        self.step.coi_declaration_text = "The step's."
        self.step.save()
        document, _ = call_transfer.export_call(self.call)
        self.assertEqual(document["call"]["coi_declaration_text"], "The call's.")
        exported = next(
            s for s in document["workflow_steps"] if s["key"] == self.step.key
        )
        self.assertEqual(exported["coi_declaration_text"], "The step's.")

        copy, _, _ = call_transfer.import_call(
            document, self.call.manager, self.fixture.staff
        )
        self.assertEqual(copy.coi_declaration_text, "The call's.")
        self.assertEqual(
            copy.workflow_steps.get(key=self.step.key).coi_declaration_text,
            "The step's.",
        )
