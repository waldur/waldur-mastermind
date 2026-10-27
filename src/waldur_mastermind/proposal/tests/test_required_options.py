"""Required offering options on proposals.

github.com/waldur/waldur-homeport/issues/129: a Slurm offering's required QoS
could be left out of a request, and the proposal was submitted, decided and
allocated without it. A draft may still leave an option for later, but
submission, the decision and call resource templates ask for it now:
``allocate_proposal`` creates the order without the marketplace serializer
that would have refused it.
"""

from types import SimpleNamespace

from rest_framework import status, test

from waldur_core.permissions.fixtures import ProposalRole
from waldur_mastermind.proposal import models, workflow_service
from waldur_mastermind.proposal.enums import ProposalStates, WorkflowStepOutcomes
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_awarded_resources import (
    AwardedResourcesBase,
)

QOS = {
    "type": "select_string",
    "label": "QoS",
    "required": True,
    "choices": ["parallel", "balanced", "long"],
}


def offering_options(**options):
    return {"order": list(options), "options": options}


class MissingRequiredOptionsTest(test.APITestCase):
    def setUp(self):
        self.offering = fixtures.ProposalFixture().offering

    def missing(self, attributes, **options):
        self.offering.options = offering_options(**options)
        return models.missing_required_options(self.offering, attributes)

    def test_reports_an_empty_required_option_by_its_label(self):
        self.assertEqual(self.missing({}, qos=QOS), ["QoS"])
        self.assertEqual(self.missing(None, qos=QOS), ["QoS"])

    def test_accepts_a_value(self):
        self.assertEqual(self.missing({"qos": "long"}, qos=QOS), [])

    def test_an_option_with_a_default_is_not_required(self):
        self.assertEqual(self.missing({}, qos={**QOS, "default": "balanced"}), [])

    def test_an_option_hidden_by_its_rule_is_not_required(self):
        mode = {"type": "select_string", "label": "Mode", "choices": ["hpc", "cloud"]}
        rule = {"field": "mode", "values": ["hpc"]}
        qos = {**QOS, "visible_if": rule}
        self.assertEqual(self.missing({"mode": "cloud"}, mode=mode, qos=qos), [])
        self.assertEqual(self.missing({"mode": "hpc"}, mode=mode, qos=qos), ["QoS"])

    def test_an_unticked_boolean_is_an_answer(self):
        # As the order form treats it: the frontend never marks it missing.
        exclusive = {"type": "boolean", "label": "Exclusive", "required": True}
        self.assertEqual(self.missing({}, exclusive=exclusive), [])

    def test_an_offering_without_options_requires_nothing(self):
        self.offering.options = {}
        self.assertEqual(models.missing_required_options(self.offering, {}), [])


class SubmissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.fixture.offering.options = offering_options(qos=QOS)
        self.fixture.offering.save()
        self.requested_resource = self.fixture.requested_resource
        self.requested_resource.attributes = {}
        self.requested_resource.save()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)

    def test_a_draft_may_leave_it_for_later(self):
        # Saving the request is not refused; only sending the proposal is.
        url = factories.RequestedResourceFactory.get_url(
            self.proposal, self.requested_resource
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(url, {"attributes": {}}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_can_submit_names_the_offering_and_the_option(self):
        can_submit, error = self.proposal.can_submit()
        self.assertFalse(can_submit)
        self.assertIn(f"{self.fixture.offering.name} (QoS)", str(error))

    def test_submission_is_refused(self):
        self.client.force_authenticate(self.fixture.proposal_creator)
        response = self.client.post(
            factories.ProposalFactory.get_url(self.proposal, "submit")
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("QoS", str(response.data))
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)

    def test_a_chosen_option_satisfies_the_check(self):
        self.requested_resource.attributes = {"qos": "long"}
        self.requested_resource.save()
        self.assertEqual(self.proposal.offerings_missing_required_options(), [])


class DecisionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.fixture.offering.options = offering_options(qos=QOS)
        self.fixture.offering.save()
        self.proposal = self.fixture.proposal
        self.awarded = models.AwardedResource.objects.create(
            proposal=self.proposal,
            requested_offering=self.fixture.requested_offering_accepted,
            attributes={},
        )
        self.decision = SimpleNamespace(step=workflow_service.DECISION_STEP)

    def test_an_award_without_the_option_cannot_be_approved(self):
        # A call manager may change an award's options, but not drop one.
        with self.assertRaisesRegex(ValueError, "required options are missing"):
            workflow_service._enforce_award_amounts(
                self.proposal, self.decision, WorkflowStepOutcomes.APPROVED
            )

    def test_declining_is_never_blocked(self):
        workflow_service._enforce_award_amounts(
            self.proposal, self.decision, WorkflowStepOutcomes.DECLINED
        )

    def test_an_award_with_the_option_passes(self):
        self.awarded.attributes = {"qos": "balanced"}
        self.awarded.save()
        self.assertEqual(self.proposal.offerings_missing_awarded_options(), [])


class CallResourceTemplateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.fixture.offering.options = offering_options(qos=QOS)
        self.fixture.offering.save()
        self.call = self.fixture.call
        self.requested_offering = self.fixture.requested_offering_accepted
        self.client.force_authenticate(self.fixture.staff)

    def create(self, attributes):
        return self.client.post(
            factories.CallResourceTemplateFactory.get_list_url(self.call),
            {
                "name": "Standard",
                "requested_offering": factories.RequestedOfferingFactory.get_url(
                    call=self.call, requested_offering=self.requested_offering
                ),
                "attributes": attributes,
            },
            format="json",
        )

    def test_a_template_must_carry_its_required_options(self):
        # Requests made from it take its values unchanged, so nothing could
        # add the option later.
        response = self.create({})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("QoS", str(response.data["attributes"]))

    def test_a_complete_template_is_saved(self):
        response = self.create({"qos": "parallel"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class WhatCountsAsMissingTest(test.APITestCase):
    """The answers an order refuses as empty, and no others."""

    def setUp(self):
        self.offering = fixtures.ProposalFixture().offering

    def missing(self, attributes, **options):
        self.offering.options = offering_options(**options)
        return models.missing_required_options(self.offering, attributes)

    def test_null_and_blank_are_missing(self):
        self.assertEqual(self.missing({"qos": None}, qos=QOS), ["QoS"])
        account = {"type": "string", "label": "Account", "required": True}
        self.assertEqual(self.missing({"account": ""}, account=account), ["Account"])

    def test_blank_text_is_missing(self):
        account = {"type": "string", "label": "Account", "required": True}
        self.assertEqual(self.missing({"account": "  "}, account=account), ["Account"])

    def test_another_options_value_does_not_hide_it(self):
        # Checked one by one: a value another check would refuse does not
        # mask the empty option beside it.
        account = {"type": "string", "label": "Account", "pattern": "^[a-z]+$"}
        self.assertEqual(
            self.missing({"account": "NOT VALID"}, account=account, qos=QOS), ["QoS"]
        )

    def test_an_empty_multiple_choice_is_an_answer(self):
        # A marketplace order accepts it too.
        partitions = {
            "type": "select_string_multi",
            "label": "Partitions",
            "required": True,
            "choices": ["cpu", "gpu"],
        }
        self.assertEqual(self.missing({"partitions": []}, partitions=partitions), [])


class AwardEditorTest(AwardedResourcesBase):
    """The award editor refuses a missing required option, as it does a
    missing amount, rather than leave it to the decision."""

    def setUp(self):
        super().setUp()
        self.offering.options = offering_options(qos=QOS)
        self.offering.save()
        self.requested_resource.attributes = {"qos": "parallel"}
        self.requested_resource.save()
        self._start_allocation()
        self.awarded = self._awarded()

    def patch(self, attributes):
        self.client.force_authenticate(self.call_manager)
        return self.client.patch(
            self._detail_url(self.awarded), {"attributes": attributes}, format="json"
        )

    def test_dropping_the_option_is_refused(self):
        response = self.patch({})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("QoS", str(response.data["attributes"]))

    def test_any_save_reports_it_as_amounts_are(self):
        # An award seeded without the option, or whose offering gained it
        # later: editing only its amounts says what is missing.
        self.awarded.attributes = {}
        self.awarded.save()
        self.client.force_authenticate(self.call_manager)
        response = self.client.patch(
            self._detail_url(self.awarded), {"limits": {"cpu": 40}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("QoS", str(response.data["attributes"]))

    def test_changing_it_is_allowed(self):
        response = self.patch({"qos": "balanced"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
