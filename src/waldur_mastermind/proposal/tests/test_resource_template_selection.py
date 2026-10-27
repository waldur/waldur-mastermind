import datetime
from unittest import mock

from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
    ResourceTemplateSelectionModes,
)
from waldur_mastermind.proposal.serializers import (
    ProtectedCallSerializer,
    RequestedResourceSerializer,
)
from waldur_mastermind.proposal.tests import factories as proposal_factories
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures


class ResourceTemplateSelectionTestCase(APITestCase):
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
            call=self.call, requested_offering=self.requested_offering, name="Basic"
        )
        self.long = proposal_factories.CallResourceTemplateFactory(
            call=self.call, requested_offering=self.requested_offering, name="Long"
        )
        self.proposal = proposal_factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )
        self.client.force_authenticate(self.staff)

    def set_mode(self, mode):
        self.call.resource_template_selection_mode = mode
        self.call.save(update_fields=["resource_template_selection_mode"])

    def request_template(self, template, proposal=None):
        proposal = proposal or self.proposal
        return self.client.post(
            proposal_factories.RequestedResourceFactory.get_list_url(proposal),
            {"call_resource_template_uuid": template.uuid.hex},
            format="json",
        )

    def request_offering(self, proposal=None):
        proposal = proposal or self.proposal
        return self.client.post(
            proposal_factories.RequestedResourceFactory.get_list_url(proposal),
            {"requested_offering_uuid": self.requested_offering.uuid.hex},
            format="json",
        )

    def add_direct_request(self, proposal=None):
        # Template calls refuse these through the API; they still exist from
        # before the rule, or when a request's template was deleted.
        return proposal_factories.RequestedResourceFactory(
            proposal=proposal or self.proposal,
            requested_offering=self.requested_offering,
        )

    def update_mode(self, mode):
        return self.client.patch(
            proposal_factories.CallFactory.get_protected_url(self.call),
            {"resource_template_selection_mode": mode},
            format="json",
        )

    def assert_mode(self, mode):
        self.call.refresh_from_db()
        self.assertEqual(self.call.resource_template_selection_mode, mode)

    def test_mode_defaults_to_multiple(self):
        self.assertEqual(
            self.call.resource_template_selection_mode,
            ResourceTemplateSelectionModes.MULTIPLE,
        )

    def test_mode_is_public(self):
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)
        response = self.client.get(
            proposal_factories.CallFactory.get_public_url(self.call)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["resource_template_selection_mode"],
            ResourceTemplateSelectionModes.SINGLE,
        )

    def test_multiple_mode_allows_several_templates(self):
        self.assertEqual(self.request_template(self.basic).status_code, 201)
        self.assertEqual(self.request_template(self.long).status_code, 201)
        self.assertEqual(self.proposal.requestedresource_set.count(), 2)

    def test_single_mode_rejects_second_template(self):
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)
        self.assertEqual(self.request_template(self.basic).status_code, 201)

        response = self.request_template(self.long)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("only one resource template", str(response.data))
        self.assertEqual(self.proposal.requestedresource_set.count(), 1)

    def test_single_mode_rejects_direct_request_beside_template(self):
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)
        self.assertEqual(self.request_template(self.basic).status_code, 201)

        response = self.request_offering()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.proposal.requestedresource_set.count(), 1)

    def test_single_mode_rejects_template_beside_direct_request(self):
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)
        self.add_direct_request()

        response = self.request_template(self.basic)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.proposal.requestedresource_set.count(), 1)

    def test_single_mode_counts_request_of_deleted_template(self):
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)
        self.request_template(self.basic)
        self.basic.delete()

        response = self.request_template(self.long)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_single_mode_without_templates_does_not_limit_requests(self):
        models.CallResourceTemplate.objects.filter(call=self.call).delete()
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)

        self.assertEqual(self.request_offering().status_code, 201)
        self.assertEqual(self.request_offering().status_code, 201)

    def test_single_mode_rejects_same_template_twice(self):
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)
        self.assertEqual(self.request_template(self.basic).status_code, 201)
        self.assertEqual(self.request_template(self.basic).status_code, 400)

    def test_single_mode_replaces_after_removal(self):
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)
        first = self.request_template(self.basic)
        resource = models.RequestedResource.objects.get(uuid=first.data["uuid"])

        response = self.client.delete(
            proposal_factories.RequestedResourceFactory.get_url(self.proposal, resource)
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.assertEqual(self.request_template(self.long).status_code, 201)
        self.assertEqual(
            list(
                self.proposal.requestedresource_set.values_list(
                    "call_resource_template", flat=True
                )
            ),
            [self.long.id],
        )

    def test_single_mode_counts_per_proposal(self):
        self.set_mode(ResourceTemplateSelectionModes.SINGLE)
        other = proposal_factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )
        self.assertEqual(self.request_template(self.basic).status_code, 201)
        self.assertEqual(self.request_template(self.basic, other).status_code, 201)

    def test_switch_to_single_refused_while_draft_has_several_templates(self):
        self.request_template(self.basic)
        self.request_template(self.long)

        response = self.update_mode(ResourceTemplateSelectionModes.SINGLE)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("resource_template_selection_mode", response.data)
        self.call.refresh_from_db()
        self.assertEqual(
            self.call.resource_template_selection_mode,
            ResourceTemplateSelectionModes.MULTIPLE,
        )

    def test_switch_to_single_refused_while_draft_mixes_template_and_direct(self):
        self.request_template(self.basic)
        self.add_direct_request()

        response = self.update_mode(ResourceTemplateSelectionModes.SINGLE)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_switch_to_single_rechecked_on_save(self):
        # A request added after the PATCH was validated must still stop it,
        # so update() checks again rather than trusting validation.
        self.request_template(self.basic)
        self.request_template(self.long)

        with self.assertRaises(ValidationError):
            ProtectedCallSerializer().update(
                self.call,
                {
                    "resource_template_selection_mode": ResourceTemplateSelectionModes.SINGLE
                },
            )

        self.call.refresh_from_db()
        self.assertEqual(
            self.call.resource_template_selection_mode,
            ResourceTemplateSelectionModes.MULTIPLE,
        )

    def test_switch_to_single_allowed_when_only_submitted_have_several(self):
        self.request_template(self.basic)
        self.request_template(self.long)
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save(update_fields=["state"])

        response = self.update_mode(ResourceTemplateSelectionModes.SINGLE)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_mode(ResourceTemplateSelectionModes.SINGLE)

    def test_switch_to_single_allowed_with_one_template_per_draft(self):
        self.request_template(self.basic)

        response = self.update_mode(ResourceTemplateSelectionModes.SINGLE)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assert_mode(ResourceTemplateSelectionModes.SINGLE)

    def test_model_clean_refuses_switch_to_single(self):
        self.request_template(self.basic)
        self.request_template(self.long)
        self.call.resource_template_selection_mode = (
            ResourceTemplateSelectionModes.SINGLE
        )

        with self.assertRaises(ValidationError):
            self.call.clean()

    def test_create_rechecks_draft_state_under_lock(self):
        # validate_proposal ran before the lock; a submit landing in between
        # must still stop the insert.
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save(update_fields=["state"])
        serializer = RequestedResourceSerializer(
            context={"request": mock.Mock(user=self.staff)}
        )

        with self.assertRaises(ValidationError):
            serializer.create(
                {
                    "proposal": self.proposal,
                    "requested_offering": self.requested_offering,
                    "call_resource_template": self.basic,
                }
            )

        self.assertFalse(self.proposal.requestedresource_set.exists())


class ResourceTemplateSelectionPermissionTestCase(APITestCase):
    def setUp(self):
        self.fixture = proposal_fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = proposal_factories.CallFactory.get_protected_url(self.call)

    def update_mode_as(self, user):
        self.client.force_authenticate(user)
        return self.client.patch(
            self.url,
            {"resource_template_selection_mode": ResourceTemplateSelectionModes.SINGLE},
            format="json",
        )

    def test_call_manager_can_set_mode(self):
        response = self.update_mode_as(self.fixture.call_manager)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertEqual(
            self.call.resource_template_selection_mode,
            ResourceTemplateSelectionModes.SINGLE,
        )

    def test_applicant_cannot_set_mode(self):
        response = self.update_mode_as(self.fixture.proposal_creator)

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.call.refresh_from_db()
        self.assertEqual(
            self.call.resource_template_selection_mode,
            ResourceTemplateSelectionModes.MULTIPLE,
        )
