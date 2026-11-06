"""Call panels: routing a proposal to the panel covering its domain, and
staffing its evaluator roles from that panel's chair and members."""

from django.core import mail
from django.test import override_settings
from rest_framework import status, test

from waldur_core.core.models import Notification
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_transfer,
    contributions,
    models,
    proposal_roles,
    utils,
)
from waldur_mastermind.proposal.enums import (
    ProposalRoleEligibility,
    ProposalStates,
    ResponsibleRoles,
    StepKinds,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_call_steps import CallStepsTestMixin
from waldur_mastermind.proposal.tests.test_proposal_roles import call_url


class PanelsTestMixin(CallStepsTestMixin):
    """Two panels, physics and chemistry, each with a chair and members; a
    chair assignment step after the administrative check, then the
    rapporteurs' assignment."""

    def setUp(self):
        super().setUp()
        self.manager = self.fixture.call_manager
        self.physics = structure_factories.ScienceDomainFactory(name="Physics")
        self.astro = structure_factories.ScienceSubDomainFactory(
            domain=self.physics, name="Astrophysics"
        )
        self.plasma = structure_factories.ScienceSubDomainFactory(
            domain=self.physics, name="Plasma"
        )
        self.chemistry = structure_factories.ScienceDomainFactory(name="Chemistry")
        self.materials = structure_factories.ScienceSubDomainFactory(
            domain=self.chemistry, name="Materials"
        )
        self.p_chair, self.p_1, self.p_2 = (self.panel_member() for _ in range(3))
        self.c_chair, self.c_1, self.c_2 = (self.panel_member() for _ in range(3))
        self.physics_panel = self.make_panel(
            "Physics", self.p_chair, [self.p_1, self.p_2], domains=[self.physics]
        )
        self.chemistry_panel = self.make_panel(
            "Chemistry",
            self.c_chair,
            [self.c_1, self.c_2],
            domains=[self.chemistry],
            sub_domains=[self.plasma],
        )
        self.chair_role = models.CallProposalRole.objects.create(
            call=self.call,
            key="domain_panel_chair",
            name="Domain Panel Chair",
            eligible_from=ProposalRoleEligibility.PANEL_CHAIR,
            min_holders=1,
            max_holders=1,
        )
        self.rapporteur = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.PANEL_MEMBER,
            min_holders=2,
            max_holders=2,
            allows_lead=True,
        )
        self.chair_step = self.add_step(
            "chair-assignment",
            StepKinds.ASSIGNMENT,
            "Domain Panel Chair assignment",
            1,
            requires_coi_confirmation=True,
        )
        self.chair_step.assigns_roles.set([self.chair_role])
        self.rapporteur_step = self.add_step(
            "rapporteur-assignment",
            StepKinds.ASSIGNMENT,
            "Rapporteur assignment",
            2,
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.chair_role,
        )
        self.rapporteur_step.assigns_roles.set([self.rapporteur])
        self.proposal.science_sub_domain = self.astro
        self.proposal.save(update_fields=["science_sub_domain"])

    def panel_member(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.PANEL_MEMBER)
        return user

    def make_panel(self, name, chair, members, domains=(), sub_domains=()):
        panel = models.CallPanel.objects.create(call=self.call, name=name, chair=chair)
        panel.members.set([chair, *members])
        panel.science_domains.set(domains)
        panel.science_sub_domains.set(sub_domains)
        return panel

    def panels_url(self, panel=None):
        url = call_url(self.call, "panels")
        return url if panel is None else f"{url}{panel.uuid.hex}/"

    def reach_chair_step(self):
        self.submit()
        response = self.complete("eligible")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.workflow_step, "chair-assignment")

    def set_panel(self, panel, user=None):
        self.client.force_authenticate(user or self.manager)
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="set_panel"),
            {"panel": panel.uuid.hex if panel else None},
            format="json",
        )

    def assign(self, user, role, by=None):
        self.client.force_authenticate(by or self.manager)
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="role_assignments"),
            {"role_uuid": role.uuid.hex, "user_uuid": user.uuid.hex},
        )

    def chair_assignment(self):
        return proposal_roles.active_assignments(self.proposal, self.chair_role).first()


class PanelConfigurationTest(PanelsTestMixin, test.APITestCase):
    def test_call_manager_defines_a_panel(self):
        member = self.panel_member()
        earth = structure_factories.ScienceDomainFactory(name="Earth")
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.panels_url(),
            {
                "name": "Earth System Sciences",
                "members": [member.uuid.hex],
                "chair": member.uuid.hex,
                "science_domains": [earth.uuid.hex],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["key"], "earth-system-sciences")
        self.assertEqual(response.data["chair"], member.uuid.hex)
        self.assertEqual(response.data["members"], [member.uuid.hex])
        panel = models.CallPanel.objects.get(uuid=response.data["uuid"])
        self.assertEqual(list(panel.science_domains.all()), [earth])

    def test_members_must_be_panel_members_of_the_call(self):
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.panels_url(),
            {"name": "Other", "members": [outsider.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("members", response.data)

    def test_chair_must_be_a_member(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            self.panels_url(self.physics_panel),
            {"name": "Physics", "chair": self.c_1.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("chair", response.data)

    def test_a_domain_goes_to_one_panel_only(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.panels_url(),
            {"name": "Again", "science_domains": [self.physics.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("science_domains", response.data)

        response = self.client.patch(
            self.panels_url(self.physics_panel),
            {"name": "Physics", "science_sub_domains": [self.plasma.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("science_sub_domains", response.data)

    def test_panel_members_read_and_reviewers_do_not(self):
        self.client.force_authenticate(self.p_1)
        response = self.client.get(self.panels_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        response = self.client.post(self.panels_url(), {"name": "Mine"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        self.client.force_authenticate(reviewer)
        self.assertIn(
            self.client.get(self.panels_url()).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertIn(
            self.client.get(self.panels_url(self.physics_panel)).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_a_panel_with_proposals_cannot_be_deleted(self):
        self.reach_chair_step()
        self.client.force_authenticate(self.manager)
        response = self.client.delete(self.panels_url(self.physics_panel))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.delete(self.panels_url(self.chemistry_panel))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_revoking_the_panel_member_role_takes_the_user_off_panels(self):
        self.call.remove_user(self.p_chair)
        self.physics_panel.refresh_from_db()
        self.assertIsNone(self.physics_panel.chair)
        self.assertNotIn(self.p_chair, self.physics_panel.members.all())

    def test_a_chair_role_has_at_most_one_holder(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            call_url(self.call, "proposal_roles"),
            {
                "name": "Chairs",
                "eligible_from": ProposalRoleEligibility.PANEL_CHAIR,
                "min_holders": 2,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("min_holders", response.data)

    def test_staffing_counts_the_smallest_panel(self):
        self.assertEqual(proposal_roles.eligible_count(self.rapporteur), 3)
        self.assertIsNone(proposal_roles.holders_shortfall(self.rapporteur))
        self.chemistry_panel.members.set([self.c_chair])
        self.assertEqual(proposal_roles.eligible_count(self.rapporteur), 1)
        self.assertIn("Chemistry", proposal_roles.holders_shortfall(self.rapporteur))
        self.chemistry_panel.chair = None
        self.chemistry_panel.save()
        self.assertEqual(proposal_roles.eligible_count(self.chair_role), 0)
        self.assertIn("no chair", proposal_roles.holders_shortfall(self.chair_role))


class RoutingTest(PanelsTestMixin, test.APITestCase):
    def test_routed_by_domain_when_evaluation_starts(self):
        self.submit()
        self.assertEqual(self.proposal.panel, self.physics_panel)
        self.assertTrue(
            Event.objects.filter(event_type="proposal_panel_changed").exists()
        )

    def test_a_sub_domain_mapping_wins_over_its_domain(self):
        self.proposal.science_sub_domain = self.plasma
        self.proposal.save()
        self.submit()
        self.assertEqual(self.proposal.panel, self.chemistry_panel)

    def test_unmapped_proposal_has_no_panel(self):
        self.proposal.science_sub_domain = structure_factories.ScienceSubDomainFactory()
        self.proposal.save()
        self.submit()
        self.assertIsNone(self.proposal.panel)

    def test_the_applicant_never_sees_the_panel(self):
        self.submit()
        self.client.force_authenticate(self.fixture.proposal_creator)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["panel_uuid"])
        self.assertIsNone(response.data["panel_name"])
        # Nor does a filter on the panel tell it.
        response = self.client.get(
            factories.ProposalFactory.get_list_url(),
            {"panel_uuid": self.physics_panel.uuid.hex},
        )
        self.assertEqual(response.data, [])

        self.client.force_authenticate(self.manager)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.data["panel_name"], "Physics")
        response = self.client.get(
            factories.ProposalFactory.get_list_url(),
            {"panel_uuid": self.physics_panel.uuid.hex},
        )
        self.assertEqual(len(response.data), 1)


class ChairAssignmentTest(PanelsTestMixin, test.APITestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def test_the_panel_chair_is_assigned_and_confirms_no_conflict(self):
        Notification.objects.update_or_create(
            key="proposal.proposal_role_assigned", defaults={"enabled": True}
        )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.reach_chair_step()
        assignment = self.chair_assignment()
        self.assertEqual(assignment.user, self.p_chair)
        self.assertTrue(assignment.coi_confirmation_required)
        self.assertIsNone(assignment.coi_confirmed_at)
        # No access until confirmed.
        self.assertFalse(
            proposal_roles.holds_role(self.p_chair, self.proposal, self.chair_role)
        )
        self.assertIn([self.p_chair.email], [m.to for m in mail.outbox])
        # The applicant is not told who chairs the evaluation.
        self.assertNotIn(
            [self.fixture.proposal_creator.email], [m.to for m in mail.outbox]
        )

    def test_a_conflicted_chair_is_not_assigned_and_the_step_says_why(self):
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.p_chair),
            proposal=self.proposal,
            call=self.call,
        )
        self.reach_chair_step()
        self.assertIsNone(self.chair_assignment())
        reason = contributions.why_step_waits(self.proposal.current_step)
        self.assertIn("cannot take this proposal", reason)

    def test_a_chair_on_the_applicant_team_is_not_assigned(self):
        self.proposal.add_user(self.p_chair, ProposalRole.MEMBER)
        self.reach_chair_step()
        self.assertIsNone(self.chair_assignment())

    def test_an_unrouted_proposal_waits_for_its_panel(self):
        self.proposal.science_sub_domain = None
        self.proposal.save()
        self.reach_chair_step()
        self.assertIsNone(self.chair_assignment())
        reason = contributions.why_step_waits(self.proposal.current_step)
        self.assertIn("set its panel", reason)

        response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.chair_assignment().user, self.c_chair)


class RapporteurEligibilityTest(PanelsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_chair_step()
        proposal_roles.confirm_no_conflict(self.chair_assignment())
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "rapporteur-assignment")

    def test_candidates_are_members_of_the_proposals_panel(self):
        self.client.force_authenticate(self.p_chair)
        response = self.client.get(
            factories.ProposalFactory.get_url(
                self.proposal, action="role_assignment_candidates"
            ),
            {"role_uuid": self.rapporteur.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertCountEqual(
            [c["uuid"] for c in response.data],
            [u.uuid.hex for u in (self.p_chair, self.p_1, self.p_2)],
        )

    def test_a_peer_is_not_told_who_has_a_recorded_conflict(self):
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.p_1),
            proposal=self.proposal,
            call=self.call,
        )
        conflicted = self.assign(self.p_1, self.rapporteur, by=self.p_chair)
        ineligible = self.assign(self.c_1, self.rapporteur, by=self.p_chair)
        self.assertEqual(conflicted.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(conflicted.data, ineligible.data)
        self.assertNotIn("conflict", str(conflicted.data))
        # Those overseeing the call are told why.
        response = self.assign(self.p_1, self.rapporteur)
        self.assertIn("conflict of interest", str(response.data))

    def test_assigning_a_member_of_another_panel_is_refused(self):
        response = self.assign(self.c_1, self.rapporteur, by=self.p_chair)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.assign(self.p_1, self.rapporteur, by=self.p_chair)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class SetPanelTest(PanelsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_chair_step()

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def test_the_workflows_chair_goes_with_the_move(self):
        for key in (
            "proposal.proposal_role_assigned",
            "proposal.proposal_role_revoked",
        ):
            Notification.objects.update_or_create(key=key, defaults={"enabled": True})
        old = self.chair_assignment()
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        old.refresh_from_db()
        self.assertIsNotNone(old.revoked_at)
        self.assertEqual(old.revoked_reason, "Moved to panel Chemistry.")
        new = self.chair_assignment()
        self.assertEqual(new.user, self.c_chair)
        self.assertTrue(new.coi_confirmation_required)
        self.assertIsNone(new.assigned_by)
        recipients = [m.to for m in mail.outbox]
        self.assertIn([self.p_chair.email], recipients)
        self.assertIn([self.c_chair.email], recipients)
        self.assertTrue(
            Event.objects.filter(event_type="proposal_evaluator_revoked").exists()
        )

    def test_a_conflicted_new_chair_is_not_assigned(self):
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.c_chair),
            proposal=self.proposal,
            call=self.call,
        )
        response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(self.chair_assignment())

    def test_refused_once_the_chair_has_worked_on_it(self):
        proposal_roles.confirm_no_conflict(self.chair_assignment())
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        response = self.assign(self.p_1, self.rapporteur, by=self.p_chair)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The rapporteur alone: assigned by hand.
        chair = self.chair_assignment()
        chair.revoked_at = chair.created
        chair.save(update_fields=["revoked_at"])
        response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("by hand", str(response.data))
        chair.revoked_at = None
        chair.save(update_fields=["revoked_at"])
        # The chair alone: they completed a step and assigned someone.
        proposal_roles.revoke(
            proposal_roles.active_assignments(self.proposal, self.rapporteur).get(),
            self.manager,
        )
        response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already worked on this proposal", str(response.data))
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.panel, self.physics_panel)
        self.assertIsNone(self.chair_assignment().revoked_at)

    def test_refused_while_a_chair_assigned_by_hand_holds_it(self):
        proposal_roles.revoke(self.chair_assignment(), self.manager)
        response = self.assign(self.p_chair, self.chair_role)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("by hand", str(response.data))
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.panel, self.physics_panel)

    def test_moved_once_the_holders_are_revoked(self):
        proposal_roles.revoke(self.chair_assignment(), self.manager)
        response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["panel_name"], "Chemistry")
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.panel, self.chemistry_panel)
        # The running assignment step takes the new panel's chair.
        self.assertEqual(self.chair_assignment().user, self.c_chair)
        self.assertTrue(
            Event.objects.filter(
                event_type="proposal_panel_changed",
                message__contains="from Physics to Chemistry",
            ).exists()
        )

    def test_the_steps_completer_moves_it(self):
        self.chair_step.completer = ResponsibleRoles.PANEL_CHAIR
        self.chair_step.save(update_fields=["completer"])
        arc_chair = self.panel_member()
        self.call.panel_chair = arc_chair
        self.call.save(update_fields=["panel_chair"])
        proposal_roles.revoke(self.chair_assignment(), self.manager)
        response = self.set_panel(self.chemistry_panel, user=arc_chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_others_cannot_move_it(self):
        proposal_roles.revoke(self.chair_assignment(), self.manager)
        for user in (self.p_1, self.fixture.proposal_creator):
            response = self.set_panel(self.chemistry_panel, user=user)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )

    def test_a_panel_of_another_call_is_refused(self):
        proposal_roles.revoke(self.chair_assignment(), self.manager)
        other = models.CallPanel.objects.create(
            call=factories.CallFactory(manager=self.call.manager), name="Other"
        )
        response = self.set_panel(other)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("panel", response.data)

    def test_not_moved_once_decided(self):
        self.proposal.state = ProposalStates.ACCEPTED
        self.proposal.save()
        response = self.set_panel(self.chemistry_panel)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PanelTransferTest(PanelsTestMixin, test.APITestCase):
    def test_panels_travel_by_domain_codes_without_people(self):
        data, warnings = call_transfer.export_call(self.call)
        self.assertEqual(warnings, [])
        exported = {p["key"]: p for p in data["panels"]}
        self.assertEqual(
            exported["chemistry"]["science_domains"],
            [{"code": self.chemistry.code, "name": self.chemistry.name}],
        )
        self.assertEqual(
            exported["chemistry"]["science_sub_domains"],
            [{"code": self.plasma.code, "name": self.plasma.name}],
        )
        self.assertNotIn("chair", exported["physics"])
        self.assertNotIn("members", exported["physics"])

        data["panels"].append(
            {"key": "unknown", "name": "Unknown", "science_domains": ["no-such"]}
        )
        call, _, warnings = call_transfer.import_call(
            data, self.call.manager, self.fixture.staff
        )
        self.assertEqual(
            sorted(call.panels.values_list("key", flat=True)),
            ["chemistry", "physics", "unknown"],
        )
        imported = call.panels.get(key="chemistry")
        self.assertEqual(list(imported.science_sub_domains.all()), [self.plasma])
        self.assertIsNone(imported.chair)
        self.assertFalse(imported.members.exists())
        self.assertTrue(any("no-such" in w for w in warnings))

    def import_physics_panel(self, entry):
        data, _ = call_transfer.export_call(self.call)
        data["panels"] = [
            {"key": "physics", "name": "Physics", "science_domains": [entry]}
        ]
        call, _, warnings = call_transfer.import_call(
            data, self.call.manager, self.fixture.staff
        )
        return call.panels.get(key="physics"), warnings

    def test_a_domain_is_matched_by_code_and_name(self):
        panel, warnings = self.import_physics_panel(
            {"code": self.physics.code, "name": self.physics.name}
        )
        self.assertEqual(list(panel.science_domains.all()), [self.physics])
        self.assertEqual(warnings, [])

    def test_a_code_this_portal_gives_another_name_is_not_mapped(self):
        panel, warnings = self.import_physics_panel(
            {"code": self.physics.code, "name": "Mathematics, computer science"}
        )
        self.assertFalse(panel.science_domains.exists())
        self.assertEqual(
            warnings,
            [
                f"Science domain '{self.physics.code}' is '{self.physics.name}' "
                "here, the imported call expects 'Mathematics, computer "
                "science', skipped."
            ],
        )

    def test_a_bare_code_of_an_older_document_is_matched_by_code(self):
        panel, warnings = self.import_physics_panel(self.physics.code)
        self.assertEqual(list(panel.science_domains.all()), [self.physics])
        self.assertEqual(len(warnings), 1)
        self.assertIn("by code only", warnings[0])

    def test_duplicated_call_keeps_its_panels_without_people(self):
        new_call = utils.duplicate_call(self.call, "Copy", self.fixture.staff)
        panel = new_call.panels.get(key="physics")
        self.assertEqual(list(panel.science_domains.all()), [self.physics])
        self.assertFalse(panel.members.exists())
