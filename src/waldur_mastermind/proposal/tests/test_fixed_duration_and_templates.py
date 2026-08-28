"""
Tests for fixed duration and resource templates functionality.
Uses Factory pattern for cleaner test setup.
"""

import datetime

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
)
from waldur_mastermind.proposal.tests import factories as proposal_factories


class CallResourceTemplateTestCase(APITestCase):
    """Test CRUD operations for call resource templates."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.call = proposal_factories.CallFactory()
        self.requested_offering = proposal_factories.RequestedOfferingFactory(
            call=self.call, state=RequestedOfferingStates.ACCEPTED
        )

    def test_create_resource_template(self):
        """Test creating a new resource template for a call."""
        self.client.force_authenticate(self.staff_user)
        url = proposal_factories.CallResourceTemplateFactory.get_list_url(self.call)
        # get url for self.requested_offering
        requested_offering_url = proposal_factories.RequestedOfferingFactory.get_url(
            call=self.call, requested_offering=self.requested_offering
        )

        payload = {
            "name": "Standard VM Template",
            "description": "Standard virtual machine configuration",
            "requested_offering": requested_offering_url,
            "attributes": {"cpu": 2, "ram": 4096},
            "limits": {"storage": 100},
            "is_required": True,
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # Verify template was created correctly
        template = models.CallResourceTemplate.objects.get(uuid=response.data["uuid"])
        self.assertEqual(template.name, "Standard VM Template")
        self.assertEqual(template.attributes, {"cpu": 2, "ram": 4096})
        self.assertEqual(template.limits, {"storage": 100})
        self.assertTrue(template.is_required)
        self.assertEqual(template.call, self.call)
        self.assertEqual(template.requested_offering, self.requested_offering)

    def test_list_resource_templates(self):
        """Test listing resource templates for a call."""
        # Create multiple templates using factories
        self.template1 = proposal_factories.CallResourceTemplateFactory(
            call=self.call,
            requested_offering=self.requested_offering,
            name="Standard Template",
            is_required=True,
        )
        self.template2 = proposal_factories.CallResourceTemplateFactory(
            call=self.call,
            requested_offering=self.requested_offering,
            name="Advanced Template",
            is_required=False,
        )

        self.client.force_authenticate(self.staff_user)
        url = proposal_factories.CallResourceTemplateFactory.get_list_url(self.call)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

        # Check template names are present
        template_names = {t["name"] for t in response.data}
        self.assertIn("Standard Template", template_names)
        self.assertIn("Advanced Template", template_names)

    def test_update_resource_template(self):
        """Test updating a resource template."""
        template = proposal_factories.CallResourceTemplateFactory(
            call=self.call, requested_offering=self.requested_offering
        )

        self.client.force_authenticate(self.staff_user)
        url = proposal_factories.CallResourceTemplateFactory.get_url(
            call=self.call, template=template
        )

        payload = {
            "name": "Updated Template Name",
            "description": "Updated description",
            "attributes": {"cpu": 4, "ram": 8192},
            "is_required": True,
        }

        response = self.client.patch(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        template.refresh_from_db()
        self.assertEqual(template.name, "Updated Template Name")
        self.assertEqual(template.attributes, {"cpu": 4, "ram": 8192})
        self.assertTrue(template.is_required)

    def test_delete_resource_template(self):
        """Test deleting a resource template."""
        template = proposal_factories.CallResourceTemplateFactory(
            call=self.call, requested_offering=self.requested_offering
        )

        self.client.force_authenticate(self.staff_user)
        url = proposal_factories.CallResourceTemplateFactory.get_url(
            call=self.call, template=template
        )

        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify template was deleted
        self.assertFalse(
            models.CallResourceTemplate.objects.filter(uuid=template.uuid).exists()
        )


class FixedDurationTestCase(APITestCase):
    """Test fixed duration functionality for calls."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.customer_owner = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.customer_owner, CustomerRole.OWNER)

        self.call = proposal_factories.CallFactory(state=CallStates.ACTIVE)
        self.round = proposal_factories.RoundFactory(
            call=self.call, start_time=timezone.now() - datetime.timedelta(days=1)
        )

    def test_set_fixed_duration_on_call(self):
        """Test setting fixed duration on a call."""
        self.client.force_authenticate(self.staff_user)
        url = proposal_factories.CallFactory.get_protected_url(self.call)

        payload = {"fixed_duration_in_days": 30}
        response = self.client.patch(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.call.refresh_from_db()
        self.assertEqual(self.call.fixed_duration_in_days, 30)

    def test_proposal_inherits_fixed_duration(self):
        """Test that proposals automatically inherit call's fixed duration."""
        self.call.fixed_duration_in_days = 45
        self.call.save()

        self.client.force_authenticate(self.customer_owner)
        url = proposal_factories.ProposalFactory.get_list_url()

        payload = {
            "name": "Test Proposal",
            "round_uuid": self.round.uuid.hex,
            "description": "Test description",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        proposal = models.Proposal.objects.get(uuid=response.data["uuid"])
        self.assertEqual(proposal.duration_in_days, 45)

    def test_fixed_duration_overrides_provided_value(self):
        """Test that fixed duration overrides any provided duration value."""
        # Set fixed duration on call
        self.call.fixed_duration_in_days = 60
        self.call.save()

        self.client.force_authenticate(self.customer_owner)
        url = proposal_factories.ProposalFactory.get_list_url()

        payload = {
            "name": "Test Proposal",
            "round_uuid": self.round.uuid.hex,
            "description": "Test description",
            "duration_in_days": 30,  # This should be ignored
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        proposal = models.Proposal.objects.get(uuid=response.data["uuid"])
        # Should use call's fixed duration, not the provided value
        self.assertEqual(proposal.duration_in_days, 60)


class FixedDurationPropagationTestCase(APITestCase):
    """Test propagation of a call's fixed duration to its proposals."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.call = proposal_factories.CallFactory(
            state=CallStates.ACTIVE, fixed_duration_in_days=30
        )
        self.round = proposal_factories.RoundFactory(
            call=self.call, start_time=timezone.now() - datetime.timedelta(days=1)
        )
        self.url = proposal_factories.CallFactory.get_protected_url(self.call)
        self.client.force_authenticate(self.staff_user)

    def create_proposal(self, state):
        return proposal_factories.ProposalFactory(
            round=self.round, state=state, duration_in_days=30
        )

    def test_pending_proposals_are_updated(self):
        pending = [
            self.create_proposal(state)
            for state in (
                ProposalStates.DRAFT,
                ProposalStates.SUBMITTED,
                ProposalStates.IN_REVIEW,
            )
        ]

        response = self.client.patch(
            self.url,
            {"fixed_duration_in_days": 45, "confirm_duration_propagation": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for proposal in pending:
            proposal.refresh_from_db()
            self.assertEqual(proposal.duration_in_days, 45)

    def test_allocated_proposals_are_not_updated(self):
        allocated = [
            self.create_proposal(state)
            for state in (
                ProposalStates.ACCEPTED,
                ProposalStates.REJECTED,
                ProposalStates.CANCELED,
            )
        ]

        response = self.client.patch(
            self.url,
            {"fixed_duration_in_days": 45, "confirm_duration_propagation": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for proposal in allocated:
            proposal.refresh_from_db()
            self.assertEqual(proposal.duration_in_days, 30)

    def test_clearing_fixed_duration_propagates_null(self):
        pending = self.create_proposal(ProposalStates.SUBMITTED)
        accepted = self.create_proposal(ProposalStates.ACCEPTED)

        response = self.client.patch(
            self.url,
            {"fixed_duration_in_days": None, "confirm_duration_propagation": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.call.refresh_from_db()
        self.assertIsNone(self.call.fixed_duration_in_days)
        pending.refresh_from_db()
        self.assertIsNone(pending.duration_in_days)
        accepted.refresh_from_db()
        self.assertEqual(accepted.duration_in_days, 30)

    def test_change_is_rejected_without_confirmation(self):
        proposal = self.create_proposal(ProposalStates.DRAFT)

        response = self.client.patch(
            self.url, {"fixed_duration_in_days": 45}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("confirm_duration_propagation", response.data)
        self.call.refresh_from_db()
        self.assertEqual(self.call.fixed_duration_in_days, 30)
        proposal.refresh_from_db()
        self.assertEqual(proposal.duration_in_days, 30)

    def test_confirmation_is_not_required_without_affected_proposals(self):
        allocated = self.create_proposal(ProposalStates.ACCEPTED)

        response = self.client.patch(
            self.url, {"fixed_duration_in_days": 45}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.call.refresh_from_db()
        self.assertEqual(self.call.fixed_duration_in_days, 45)
        allocated.refresh_from_db()
        self.assertEqual(allocated.duration_in_days, 30)

    def test_confirmation_is_not_required_when_value_is_unchanged(self):
        self.create_proposal(ProposalStates.DRAFT)

        response = self.client.patch(
            self.url, {"fixed_duration_in_days": 30}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_positive_duration_is_rejected(self):
        response = self.client.patch(
            self.url, {"fixed_duration_in_days": 0}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("fixed_duration_in_days", response.data)


class ResourceTemplateValidationTestCase(APITestCase):
    """Test validation logic for resource templates and requests."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.customer_owner = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.customer_owner, CustomerRole.OWNER)

        self.call = proposal_factories.CallFactory(state=CallStates.ACTIVE)
        self.round = proposal_factories.RoundFactory(
            call=self.call, start_time=timezone.now() - datetime.timedelta(days=1)
        )
        self.requested_offering = proposal_factories.RequestedOfferingFactory(
            call=self.call, state=RequestedOfferingStates.ACCEPTED
        )

        # Create a resource template using factory
        self.template = proposal_factories.CallResourceTemplateFactory(
            call=self.call,
            requested_offering=self.requested_offering,
            name="Standard Template",
            attributes={"cpu": 2, "ram": 4096},
            limits={"storage": 50},
        )

    def test_create_resource_from_template(self):
        """Test creating a requested resource using a template."""
        proposal = proposal_factories.ProposalFactory(
            round=self.round, created_by=self.customer_owner, state=ProposalStates.DRAFT
        )

        self.client.force_authenticate(self.staff_user)
        url = proposal_factories.RequestedResourceFactory.get_list_url(proposal)

        payload = {
            "call_resource_template_uuid": self.template.uuid.hex,
            "description": "Test resource from template",
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        resource = models.RequestedResource.objects.get(uuid=response.data["uuid"])
        self.assertEqual(resource.call_resource_template, self.template)
        self.assertEqual(resource.requested_offering, self.requested_offering)
        self.assertEqual(resource.attributes, {"cpu": 2, "ram": 4096})
        self.assertEqual(resource.limits, {"storage": 50})

    def test_direct_offering_blocked_when_templates_exist(self):
        """Test that direct offering requests are blocked when templates exist."""
        proposal = proposal_factories.ProposalFactory(
            round=self.round, created_by=self.customer_owner, state=ProposalStates.DRAFT
        )
        other_offering = proposal_factories.RequestedOfferingFactory(
            call=self.call,
            state=RequestedOfferingStates.ACCEPTED,
        )

        self.client.force_authenticate(self.staff_user)
        url = proposal_factories.RequestedResourceFactory.get_list_url(proposal)

        # Try to create requested resource directly (should fail)
        payload = {
            "requested_offering_uuid": other_offering.uuid.hex,
            "description": "Test resource",
            "attributes": {"cpu": 1},
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Please use a resource template", str(response.data))

    def test_direct_offering_allowed_when_no_templates(self):
        """Test that direct offering requests work when no templates exist."""
        # Create a call without templates using factories
        call_without_templates = proposal_factories.CallFactory()
        round_without_templates = proposal_factories.RoundFactory(
            call=call_without_templates,
            start_time=timezone.now() - datetime.timedelta(days=1),
        )
        offering_without_templates = proposal_factories.RequestedOfferingFactory(
            call=call_without_templates, state=RequestedOfferingStates.ACCEPTED
        )

        proposal = proposal_factories.ProposalFactory(
            round=round_without_templates,
            created_by=self.customer_owner,
            state=ProposalStates.DRAFT,
        )

        self.client.force_authenticate(self.staff_user)
        url = proposal_factories.RequestedResourceFactory.get_list_url(proposal)

        payload = {
            "requested_offering_uuid": offering_without_templates.uuid.hex,
            "description": "Test resource",
            "attributes": {"cpu": 1, "ram": 2048},
            "limits": {"storage": 25},
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        resource = models.RequestedResource.objects.get(uuid=response.data["uuid"])
        self.assertEqual(resource.requested_offering, offering_without_templates)
        self.assertEqual(resource.attributes, {"cpu": 1, "ram": 2048})
        self.assertEqual(resource.limits, {"storage": 25})
        self.assertIsNone(resource.call_resource_template)


class IntegrationTestCase(APITestCase):
    """Integration tests combining fixed duration and resource templates."""

    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)

    def test_complete_workflow_with_templates_and_fixed_duration(self):
        """Test complete workflow: create call with fixed duration and templates, then create proposal."""
        # Create call with fixed duration using factory
        call = proposal_factories.CallFactory(
            fixed_duration_in_days=30, state=CallStates.ACTIVE
        )
        round_obj = proposal_factories.RoundFactory(
            call=call, start_time=timezone.now() - datetime.timedelta(days=1)
        )

        # Add offering and create template using factories
        offering = proposal_factories.RequestedOfferingFactory(
            call=call, state=RequestedOfferingStates.ACCEPTED
        )
        template = proposal_factories.CallResourceTemplateFactory(
            call=call,
            requested_offering=offering,
            name="GPU Template",
            attributes={"gpu_count": 1, "cpu": 8},
            limits={"storage": 200},
            is_required=True,
        )

        # Create proposal
        self.client.force_authenticate(self.staff_user)
        proposal_url = proposal_factories.ProposalFactory.get_list_url()

        proposal_payload = {
            "name": "ML Research Proposal",
            "round_uuid": round_obj.uuid.hex,
            "description": "Machine learning research project",
        }

        proposal_response = self.client.post(
            proposal_url, proposal_payload, format="json"
        )
        self.assertEqual(
            proposal_response.status_code,
            status.HTTP_201_CREATED,
            proposal_response.data,
        )

        proposal = models.Proposal.objects.get(uuid=proposal_response.data["uuid"])
        # Verify fixed duration was applied
        self.assertEqual(proposal.duration_in_days, 30)

        # Add resource from template
        resource_url = proposal_factories.RequestedResourceFactory.get_list_url(
            proposal
        )
        resource_payload = {
            "call_resource_template_uuid": template.uuid.hex,
            "description": "GPU workstation for ML training",
        }

        resource_response = self.client.post(
            resource_url, resource_payload, format="json"
        )
        self.assertEqual(resource_response.status_code, status.HTTP_201_CREATED)

        resource = models.RequestedResource.objects.get(
            uuid=resource_response.data["uuid"]
        )
        # Verify template attributes were applied
        self.assertEqual(resource.attributes, {"gpu_count": 1, "cpu": 8})
        self.assertEqual(resource.limits, {"storage": 200})
        self.assertEqual(resource.call_resource_template, template)

        # Verify complete proposal state
        self.assertEqual(proposal.state, ProposalStates.DRAFT)
        self.assertEqual(proposal.requestedresource_set.count(), 1)

    def test_factories_create_consistent_data(self):
        """Test that factories create data with proper relationships."""
        # Test factory relationships
        call = proposal_factories.CallFactory()
        offering = proposal_factories.RequestedOfferingFactory(call=call)
        template = proposal_factories.CallResourceTemplateFactory(
            call=call, requested_offering=offering
        )
        round_obj = proposal_factories.RoundFactory(
            call=call, start_time=timezone.now() - datetime.timedelta(days=1)
        )
        proposal = proposal_factories.ProposalFactory(round=round_obj)

        # Verify all relationships are consistent
        self.assertEqual(template.call, call)
        self.assertEqual(template.requested_offering, offering)
        self.assertEqual(offering.call, call)
        self.assertEqual(round_obj.call, call)
        self.assertEqual(proposal.round, round_obj)
        self.assertEqual(proposal.round.call, call)

    def test_template_factory_generates_unique_names(self):
        """Test that factory generates unique names for templates."""
        call = proposal_factories.CallFactory()
        offering = proposal_factories.RequestedOfferingFactory(call=call)

        # Create multiple templates
        template1 = proposal_factories.CallResourceTemplateFactory(
            call=call, requested_offering=offering
        )
        template2 = proposal_factories.CallResourceTemplateFactory(
            call=call, requested_offering=offering
        )
        template3 = proposal_factories.CallResourceTemplateFactory(
            call=call, requested_offering=offering
        )

        # Verify names are unique
        names = {template1.name, template2.name, template3.name}
        self.assertEqual(len(names), 3, "Template names should be unique")

    def test_factory_default_attributes_and_limits(self):
        """Test that factory provides reasonable default attributes."""
        template = proposal_factories.CallResourceTemplateFactory()

        # Verify default attributes exist
        self.assertIsInstance(template.attributes, dict)
        self.assertIsInstance(template.limits, dict)
        self.assertIn("cpu", template.attributes)
        self.assertIn("ram", template.attributes)
        self.assertIn("storage", template.limits)

        # Verify reasonable default values
        self.assertEqual(template.attributes["cpu"], 2)
        self.assertEqual(template.attributes["ram"], 4096)
        self.assertEqual(template.limits["storage"], 100)
