import datetime
from unittest import mock

from django.utils import timezone
from rest_framework import exceptions, status
from rest_framework.test import APITestCase

from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
    ResourceTemplateSelectionModes,
)
from waldur_mastermind.proposal.tests import factories as proposal_factories
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures
from waldur_mastermind.proposal.views import validate_resource_templates_used


class ResourceTemplateEnforcementTestCase(APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call = proposal_factories.CallFactory(state=CallStates.ACTIVE)
        self.round = proposal_factories.RoundFactory(
            call=self.call, start_time=timezone.now() - datetime.timedelta(days=1)
        )
        self.requested_offering = proposal_factories.RequestedOfferingFactory(
            call=self.call, state=RequestedOfferingStates.ACCEPTED
        )
        self.basic = proposal_factories.CallResourceTemplateFactory(
            call=self.call,
            requested_offering=self.requested_offering,
            name="Basic",
            attributes={"qos": "basic"},
            limits={"cpu": 10},
        )
        self.proposal = proposal_factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )
        self.list_url = proposal_factories.RequestedResourceFactory.get_list_url(
            self.proposal
        )
        self.client.force_authenticate(self.staff)

    def request_template(self, template):
        return self.client.post(
            self.list_url,
            {"call_resource_template_uuid": template.uuid.hex},
            format="json",
        )

    def add_direct_request(self):
        return proposal_factories.RequestedResourceFactory(
            proposal=self.proposal, requested_offering=self.requested_offering
        )

    def patch(self, resource, payload):
        return self.client.patch(
            proposal_factories.RequestedResourceFactory.get_url(
                self.proposal, resource
            ),
            payload,
            format="json",
        )

    def test_direct_request_refused_when_call_has_templates(self):
        # The offering is one a template uses, which used to be enough.
        response = self.client.post(
            self.list_url,
            {
                "requested_offering_uuid": self.requested_offering.uuid.hex,
                "limits": {"cpu": 99999},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requested_offering_uuid", response.data)
        self.assertFalse(self.proposal.requestedresource_set.exists())

    def test_same_template_twice_refused(self):
        self.assertEqual(self.request_template(self.basic).status_code, 201)

        response = self.request_template(self.basic)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("call_resource_template_uuid", response.data)
        self.assertEqual(self.proposal.requestedresource_set.count(), 1)

    def test_template_request_limits_cannot_change(self):
        created = self.request_template(self.basic)
        resource = models.RequestedResource.objects.get(uuid=created.data["uuid"])

        response = self.patch(resource, {"limits": {"cpu": 999999}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)
        resource.refresh_from_db()
        self.assertEqual(resource.limits, {"cpu": 10})

    def test_template_request_attributes_cannot_change(self):
        created = self.request_template(self.basic)
        resource = models.RequestedResource.objects.get(uuid=created.data["uuid"])

        response = self.patch(resource, {"attributes": {"qos": "long"}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attributes", response.data)

    def test_template_request_other_fields_and_same_values_can_be_saved(self):
        created = self.request_template(self.basic)
        resource = models.RequestedResource.objects.get(uuid=created.data["uuid"])

        response = self.patch(
            resource,
            {"description": "For the Q3 runs", "limits": {"cpu": 10}},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        resource.refresh_from_db()
        self.assertEqual(resource.description, "For the Q3 runs")

    def test_submit_refused_while_a_request_is_not_from_a_template(self):
        self.add_direct_request()

        with self.assertRaises(exceptions.ValidationError):
            validate_resource_templates_used(self.proposal)
        can_submit, error = self.proposal.can_submit()
        self.assertFalse(can_submit)
        self.assertIn("only accepts its resource templates", str(error))

    def test_submit_refused_after_the_template_was_deleted(self):
        self.request_template(self.basic)
        proposal_factories.CallResourceTemplateFactory(
            call=self.call, requested_offering=self.requested_offering, name="Long"
        )
        self.basic.delete()

        with self.assertRaises(exceptions.ValidationError):
            validate_resource_templates_used(self.proposal)

    def test_submit_check_passes_with_template_requests_only(self):
        self.request_template(self.basic)

        validate_resource_templates_used(self.proposal)

    def test_submit_check_ignores_calls_without_templates(self):
        self.add_direct_request()
        self.basic.delete()

        validate_resource_templates_used(self.proposal)

    def test_proposal_list_reports_the_missing_template(self):
        self.add_direct_request()

        response = self.client.get(
            proposal_factories.ProposalFactory.get_list_url(),
            {"uuid": self.proposal.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = next(
            item for item in response.data if item["uuid"] == self.proposal.uuid.hex
        )
        self.assertFalse(row["can_submit"]["can_submit"])
        self.assertIn("only accepts its resource templates", row["can_submit"]["error"])

    def set_single_mode(self):
        self.call.resource_template_selection_mode = (
            ResourceTemplateSelectionModes.SINGLE
        )
        self.call.save(update_fields=["resource_template_selection_mode"])

    def test_single_mode_requires_a_template(self):
        self.set_single_mode()

        with self.assertRaises(exceptions.ValidationError):
            validate_resource_templates_used(self.proposal)
        can_submit, error = self.proposal.can_submit()
        self.assertFalse(can_submit)
        self.assertIn("needs one of its resource templates", str(error))

    def test_single_mode_passes_with_one_template(self):
        self.set_single_mode()
        self.request_template(self.basic)

        validate_resource_templates_used(self.proposal)

    def test_single_mode_without_templates_requires_nothing(self):
        self.set_single_mode()
        self.basic.delete()

        validate_resource_templates_used(self.proposal)

    def test_multiple_mode_does_not_require_a_template(self):
        validate_resource_templates_used(self.proposal)

    def test_template_request_cannot_bring_its_own_values(self):
        response = self.client.post(
            self.list_url,
            {
                "call_resource_template_uuid": self.basic.uuid.hex,
                "limits": {"cpu": 999999},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("limits", response.data)
        self.assertFalse(self.proposal.requestedresource_set.exists())

    def test_template_request_takes_the_templates_values(self):
        response = self.client.post(
            self.list_url,
            {"call_resource_template_uuid": self.basic.uuid.hex, "limits": {"cpu": 10}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        resource = models.RequestedResource.objects.get(uuid=response.data["uuid"])
        self.assertEqual(resource.limits, {"cpu": 10})
        self.assertEqual(resource.attributes, {"qos": "basic"})

    def test_template_of_an_unconfirmed_offering_refused(self):
        self.requested_offering.state = RequestedOfferingStates.REQUESTED
        self.requested_offering.save(update_fields=["state"])

        response = self.request_template(self.basic)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not been confirmed", str(response.data))

    def test_submit_refused_with_a_template_requested_twice(self):
        for _ in range(2):
            proposal_factories.RequestedResourceFactory(
                proposal=self.proposal,
                requested_offering=self.requested_offering,
                call_resource_template=self.basic,
            )

        with self.assertRaises(exceptions.ValidationError) as raised:
            validate_resource_templates_used(self.proposal)
        self.assertIn("more than once", str(raised.exception))

    def test_template_rule_is_reported_before_missing_amounts(self):
        proposal_factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            limits={},
        )

        can_submit, error = self.proposal.can_submit()

        self.assertFalse(can_submit)
        self.assertIn("only accepts its resource templates", str(error))

    def update_template(self, template, **changes):
        # The endpoint saves the whole template, as the edit dialog sends it.
        payload = {
            "name": template.name,
            "requested_offering": proposal_factories.RequestedOfferingFactory.get_url(
                call=self.call, requested_offering=template.requested_offering
            ),
            "attributes": template.attributes,
            "limits": template.limits,
            **changes,
        }
        return self.client.put(
            proposal_factories.CallResourceTemplateFactory.get_url(self.call, template),
            payload,
            format="json",
        )

    def test_editing_a_template_updates_draft_requests_only(self):
        created = self.request_template(self.basic)
        draft_request = models.RequestedResource.objects.get(uuid=created.data["uuid"])
        submitted = proposal_factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        submitted_request = proposal_factories.RequestedResourceFactory(
            proposal=submitted,
            requested_offering=self.requested_offering,
            call_resource_template=self.basic,
            limits={"cpu": 10},
        )

        response = self.update_template(self.basic, limits={"cpu": 20})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        draft_request.refresh_from_db()
        submitted_request.refresh_from_db()
        self.assertEqual(draft_request.limits, {"cpu": 20})
        self.assertEqual(submitted_request.limits, {"cpu": 10})
        validate_resource_templates_used(self.proposal)

    def test_template_with_invalid_prepaid_duration_refused(self):
        # The offering is not sold by the month, so no request could use it.
        response = self.update_template(
            self.basic, attributes={"prepaid_duration_months": 3}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_submit_refused_when_request_no_longer_matches_template(self):
        proposal_factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            call_resource_template=self.basic,
            limits={"cpu": 999999},
            attributes=self.basic.attributes,
        )

        with self.assertRaises(exceptions.ValidationError) as raised:
            validate_resource_templates_used(self.proposal)
        self.assertIn("no longer match", str(raised.exception))

    def test_submit_refused_when_offering_no_longer_accepted(self):
        self.request_template(self.basic)
        self.requested_offering.state = RequestedOfferingStates.CANCELED
        self.requested_offering.save(update_fields=["state"])

        with self.assertRaises(exceptions.ValidationError) as raised:
            validate_resource_templates_used(self.proposal)
        self.assertIn("no longer accepts", str(raised.exception))

    def test_single_mode_submit_refused_with_several_templates(self):
        long = proposal_factories.CallResourceTemplateFactory(
            call=self.call,
            requested_offering=self.requested_offering,
            name="Long",
            attributes={"qos": "long"},
            limits={"cpu": 40},
        )
        self.request_template(self.basic)
        self.request_template(long)
        self.set_single_mode()

        with self.assertRaises(exceptions.ValidationError) as raised:
            validate_resource_templates_used(self.proposal)
        self.assertIn("one resource template per proposal", str(raised.exception))

    def test_invalid_template_reports_instead_of_raising(self):
        # A template saved before its values were validated may no longer pass
        # its offering's options; can_submit runs on every proposal list.
        self.request_template(self.basic)
        with mock.patch(
            "waldur_mastermind.proposal.utils.template_request_values",
            side_effect=exceptions.ValidationError({"attributes": "invalid"}),
        ):
            can_submit, error = self.proposal.can_submit()
            response = self.client.get(
                proposal_factories.ProposalFactory.get_list_url(),
                {"uuid": self.proposal.uuid.hex},
            )

        self.assertFalse(can_submit)
        self.assertIn("no longer match", str(error))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_template_request_echoing_the_templates_values_is_accepted(self):
        response = self.client.post(
            self.list_url,
            {
                "call_resource_template_uuid": self.basic.uuid.hex,
                "limits": {"cpu": 10},
                "attributes": {"qos": "basic"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class ResourceTemplateSubmitTestCase(APITestCase):
    def test_submit_endpoint_refuses_single_mode_draft_without_template(self):
        fixture = proposal_fixtures.ProposalFixture()
        proposal = fixture.proposal
        proposal.requestedresource_set.all().delete()
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        call = fixture.call
        call.resource_template_selection_mode = ResourceTemplateSelectionModes.SINGLE
        call.save(update_fields=["resource_template_selection_mode"])
        proposal_factories.CallResourceTemplateFactory(
            call=call,
            requested_offering=proposal_factories.RequestedOfferingFactory(
                call=call, state=RequestedOfferingStates.ACCEPTED
            ),
        )
        self.client.force_authenticate(proposal.created_by)

        response = self.client.post(
            proposal_factories.ProposalFactory.get_url(proposal, action="submit")
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("needs one of its resource templates", str(response.data))
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.DRAFT)
