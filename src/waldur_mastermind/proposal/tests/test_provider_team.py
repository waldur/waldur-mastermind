"""What a provider sees and does of its team's part in a call: the people
on the offering, the slots open on its proposals, handing them on, and the
request it accepts or declines."""

from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from waldur_core.core.models import Notification
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, OfferingRole
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import contributions, models, tasks, workflow_service
from waldur_mastermind.proposal.enums import (
    DelegationTargets,
    RequestedOfferingStates,
    StepContributionStatuses,
)

from . import factories
from .test_step_contributions import PerOfferingAssessmentBase


def provider_url(requested_offering, action=None):
    url = "http://testserver" + reverse(
        "proposal-requested-offering-detail",
        kwargs={"uuid": requested_offering.uuid.hex},
    )
    return url + f"{action}/" if action else url


class ProviderTeamBase(PerOfferingAssessmentBase):
    def setUp(self):
        # As permissions.yaml grants it: the offering's managers see and
        # answer the calls' requests for it.
        OfferingRole.MANAGER.add_permission(PermissionEnum.ACCEPT_REQUESTED_OFFERING)
        super().setUp()
        self.call_step.delegable_to = DelegationTargets.OFFERING_TEAM
        self.call_step.save()
        self.reviewer = structure_factories.UserFactory(first_name="Tech")
        # A deployment's own offering role, such as a technical reviewer.
        technical_reviewer = Role.objects.get_system_role(
            "OFFERING.TECHNICAL_REVIEWER",
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
        )
        self.offering_a.add_user(self.reviewer, technical_reviewer)

    def get(self, user, url, params=None):
        self.client.force_authenticate(user)
        return self.client.get(url, params or {})

    def post(self, user, url, data):
        self.client.force_authenticate(user)
        return self.client.post(url, data, format="json")


class TeamPeopleTest(ProviderTeamBase):
    def test_the_team_is_named(self):
        response = self.get(self.manager_a, provider_url(self.requested_a, "team"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [person["uuid"] for person in response.data["managers"]],
            [self.manager_a.uuid.hex],
        )
        self.assertEqual(
            [person["uuid"] for person in response.data["delegates"]],
            [self.reviewer.uuid.hex],
        )

    def test_a_deactivated_member_is_not(self):
        self.reviewer.is_active = False
        self.reviewer.save()
        response = self.get(self.manager_a, provider_url(self.requested_a, "team"))
        self.assertEqual(response.data["delegates"], [])


class TeamSlotsTest(ProviderTeamBase):
    def test_the_open_slots_say_who_holds_them(self):
        response = self.get(
            self.manager_a, provider_url(self.requested_a, "team_step_slots")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        (slot,) = response.data
        self.assertEqual(slot["proposal_uuid"], self.proposal.uuid.hex)
        self.assertEqual(slot["step_key"], "technical_assessment")
        self.assertIsNone(slot["delegate_name"])
        self.assertFalse(slot["stand_in"])

    def test_several_are_handed_on_at_once(self):
        slot = self._slot(self.requested_a)
        other = factories.ProposalFactory(round=self.fixture.round, state="submitted")
        factories.RequestedResourceFactory(
            proposal=other, requested_offering=self.requested_a
        )
        workflow_service.create_step_instances(other)
        workflow_service.activate_first_step(other)
        other_slot = models.StepContribution.objects.get(
            proposal_step__proposal=other, requested_offering=self.requested_a
        )
        foreign = self._slot(self.requested_b)
        response = self.post(
            self.manager_a,
            provider_url(self.requested_a, "delegate_slots"),
            {
                "contribution_uuids": [
                    slot.uuid.hex,
                    other_slot.uuid.hex,
                    foreign.uuid.hex,
                ],
                "user_uuid": self.reviewer.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            sorted(response.data["handed_on"]),
            sorted([slot.uuid.hex, other_slot.uuid.hex]),
        )
        self.assertIn(foreign.uuid.hex, response.data["skipped"])
        slot.refresh_from_db()
        self.assertEqual(slot.delegate, self.reviewer)

    def test_the_teams_open_work_across_calls(self):
        self.client.force_authenticate(self.manager_a)
        response = self.client.get(
            "http://testserver" + reverse("proposal-requested-offering-team-slots"),
            {"offering_uuid": self.offering_a.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [row["requested_offering_uuid"] for row in response.data],
            [self.requested_a.uuid.hex],
        )


class OfferingTeamViewTest(ProviderTeamBase):
    """A provider's team opens a proposal in its own view, in its provider's
    workspace: the proposal says whose, tasks and mails link there."""

    def provider_url(self):
        return tasks.offering_team_proposal_url(
            self.proposal.uuid, self.offering_a.customer.uuid
        )

    def test_the_proposal_names_the_viewers_offering_teams(self):
        # The proposal opens to a member who manages nothing once they take
        # up a slot handed to them.
        slot = self._slot(self.requested_a)
        self.post(
            self.manager_a,
            provider_url(self.requested_a, "delegate_slots"),
            {
                "contribution_uuids": [slot.uuid.hex],
                "user_uuid": self.reviewer.uuid.hex,
            },
        )
        slot.refresh_from_db()
        contributions.accept_delegation(slot, self.reviewer, confirms_no_conflict=True)
        url = factories.ProposalFactory.get_url(self.proposal)
        provider = {
            "uuid": self.offering_a.customer.uuid.hex,
            "name": self.offering_a.customer.name,
        }
        for user in (self.manager_a, self.reviewer):
            with self.subTest(user=user.first_name):
                response = self.get(user, url)
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )
                self.assertEqual(response.data["offering_team_providers"], [provider])
        response = self.get(self.proposal.created_by, url)
        self.assertEqual(response.data["offering_team_providers"], [])

    def test_a_listed_page_asks_once_for_every_proposal(self):
        other = factories.ProposalFactory(round=self.fixture.round, state="submitted")
        factories.RequestedResourceFactory(
            proposal=other, requested_offering=self.requested_a
        )
        response = self.get(
            self.manager_a,
            factories.ProposalFactory.get_list_url(),
            {"field": ["uuid", "offering_team_providers"]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        by_uuid = {row["uuid"]: row["offering_team_providers"] for row in response.data}
        for proposal in (self.proposal, other):
            self.assertEqual(
                [p["uuid"] for p in by_uuid[proposal.uuid.hex]],
                [self.offering_a.customer.uuid.hex],
            )

    def test_a_slot_handed_on_opens_in_the_offering_teams_view(self):
        slot = self._slot(self.requested_a)
        self.post(
            self.manager_a,
            provider_url(self.requested_a, "delegate_slots"),
            {
                "contribution_uuids": [slot.uuid.hex],
                "user_uuid": self.reviewer.uuid.hex,
            },
        )
        response = self.get(self.reviewer, "http://testserver/api/proposal-my-tasks/")
        (task,) = [row for row in response.data if row["proposal_uuid"]]
        self.assertEqual(
            (task["view"], task["view_customer_uuid"]),
            ("offering_team", self.offering_a.customer.uuid.hex),
        )

    def test_the_handed_on_mail_links_the_offering_teams_view(self):
        slot = self._slot(self.requested_a)
        (sent,) = tasks.contribution_delegated_mails(
            slot, "delegated", self.reviewer, self.manager_a
        )
        self.assertEqual(sent.context["proposal_url"], self.provider_url())

    def test_a_step_mail_to_its_offering_managers_links_their_view(self):
        self.call_step.completer = "offering_manager"
        self.call_step.save()
        rule = models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.call_step,
            trigger="step_started",
            recipient="responsible_role",
        )
        mails = tasks.step_event_mails(self.instance, "step_started", [rule])
        sent = next(m for m in mails if self.manager_a.email in m.recipients)
        # Not the call managers' page, which the offering's managers
        # cannot open.
        self.assertEqual(sent.context["proposal_url"], self.provider_url())

    def test_the_digest_links_the_offering_teams_view(self):
        Notification.objects.update_or_create(
            key="proposal.offering_team_digest", defaults={"enabled": True}
        )
        mail.outbox = []
        tasks.send_offering_team_digests()
        body = next(m.body for m in mail.outbox if self.manager_a.email in m.to)
        self.assertIn(self.provider_url(), body)
        self.assertNotIn("?provider=", body)


class DefaultDelegateTest(ProviderTeamBase):
    def test_new_slots_go_to_the_default_delegate(self):
        response = self.post(
            self.manager_a,
            provider_url(self.requested_a, "set_default_delegate"),
            {"user_uuid": self.reviewer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        other = factories.ProposalFactory(round=self.fixture.round, state="submitted")
        factories.RequestedResourceFactory(
            proposal=other, requested_offering=self.requested_a
        )
        workflow_service.create_step_instances(other)
        workflow_service.activate_first_step(other)
        slot = models.StepContribution.objects.get(
            proposal_step__proposal=other, requested_offering=self.requested_a
        )
        self.assertEqual(slot.delegate, self.reviewer)
        self.assertEqual(slot.status, StepContributionStatuses.PENDING)
        self.assertTrue(contributions.may_accept(self.reviewer, slot))

    def test_only_someone_on_the_offering(self):
        response = self.post(
            self.manager_a,
            provider_url(self.requested_a, "set_default_delegate"),
            {"user_uuid": structure_factories.UserFactory().uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RequestDecisionTest(ProviderTeamBase):
    def setUp(self):
        super().setUp()
        self.request = factories.RequestedOfferingFactory(
            offering=self.offering_a, state=RequestedOfferingStates.REQUESTED
        )
        models.CallOfferingComponent.objects.create(
            requested_offering=self.request,
            component_type="cores",
            max_per_proposal=64,
        )

    def test_the_provider_sees_what_it_commits_to(self):
        response = self.get(self.manager_a, provider_url(self.request))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        (rule,) = response.data["component_rules"]
        self.assertEqual(rule["component_type"], "cores")
        self.assertEqual(rule["max_per_proposal"], 64)
        self.assertEqual(response.data["pools"], [])

    def test_declining_needs_a_reason(self):
        url = provider_url(self.request, "cancel")
        response = self.post(self.manager_a, url, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.post(self.manager_a, url, {"reason": "Fully booked."})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.request.refresh_from_db()
        self.assertEqual(self.request.state, RequestedOfferingStates.CANCELED)
        self.assertEqual(self.request.rejection_reason, "Fully booked.")


class OrderAwardTest(ProviderTeamBase):
    def test_an_award_order_names_its_proposal(self):
        resource = marketplace_factories.ResourceFactory(offering=self.offering_a)
        order = marketplace_factories.OrderFactory(
            resource=resource, offering=self.offering_a, project=resource.project
        )
        models.AwardedResource.objects.create(
            proposal=self.proposal,
            requested_offering=self.requested_a,
            limits={"cores": 8},
            resource=resource,
        )
        staff = structure_factories.UserFactory(is_staff=True)
        response = self.get(staff, marketplace_factories.OrderFactory.get_url(order))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        award = response.data["award"]
        self.assertEqual(award["proposal_uuid"], self.proposal.uuid.hex)
        self.assertEqual(award["awarded_limits"], {"cores": 8})
        self.assertTrue(award["can_read"])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class ProviderNotificationsTest(ProviderTeamBase):
    def enable(self, key):
        Notification.objects.update_or_create(
            key=f"proposal.{key}", defaults={"enabled": True}
        )

    def test_the_digest_lists_new_work_to_the_team(self):
        self.enable("offering_team_digest")
        mail.outbox = []
        tasks.send_offering_team_digests()
        to = {address for message in mail.outbox for address in message.to}
        self.assertIn(self.manager_a.email, to)
        self.assertIn(self.manager_b.email, to)
        body = next(m.body for m in mail.outbox if self.manager_a.email in m.to)
        self.assertIn(self.proposal.name, body)

    def test_changed_terms_reach_the_accepted_offerings_managers(self):
        self.enable("call_offering_terms_changed")
        mail.outbox = []
        models.CallOfferingComponent.objects.create(
            requested_offering=self.requested_a,
            component_type="cores",
            max_per_proposal=16,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.manager_a.email, mail.outbox[0].to)
        self.assertIn("16", mail.outbox[0].body)

    def test_a_declined_request_tells_why(self):
        self.enable("requested_offering_decision")
        call_manager = structure_factories.UserFactory()
        request = factories.RequestedOfferingFactory(
            offering=self.offering_a, state=RequestedOfferingStates.REQUESTED
        )
        request.call.add_user(call_manager, CallRole.MANAGER)
        mail.outbox = []
        self.post(
            self.manager_a, provider_url(request, "cancel"), {"reason": "Booked out."}
        )
        (message,) = mail.outbox
        self.assertIn("rejected", message.subject)
        self.assertIn("Booked out.", message.body)
