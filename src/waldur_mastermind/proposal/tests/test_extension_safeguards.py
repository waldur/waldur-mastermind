"""An approved extension only ever extends: it never shortens the project or
a resource, never gives an open-ended project an end date, moves a resource
only on its provider's own acceptance, and a grant the extension never got
stays the deciders'."""

import datetime

from django.contrib.contenttypes.models import ContentType
from rest_framework import status

from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.proposal import contributions, workflow_service
from waldur_mastermind.proposal.enums import (
    ContributionOutcomes,
    ProposalRunStates,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests.test_extensions_and_final_report import (
    TODAY,
    PostAwardBase,
)

LATER = datetime.date(2031, 6, 30)


class ExtensionSafeguardsTest(PostAwardBase):
    def _submitted_and_assessed(self, data=None):
        run = self._request_extension(
            data
            or {
                "requested_end_date": "2030-12-31",
                "requested_limits": {self.resource.uuid.hex: {"cpu": 200}},
            }
        )
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        self._assess(run)
        return run

    def test_a_grant_the_project_has_outgrown_is_refused_when_applied(self):
        run = self._submitted_and_assessed()
        # The project's end moved past the grant after it was drafted.
        self.project.end_date = LATER
        self.project.save(update_fields=["end_date"])

        self._complete(
            self.call_manager,
            self._active(run),
            WorkflowStepOutcomes.APPROVED,
            expected=status.HTTP_400_BAD_REQUEST,
        )

        run.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.OPEN)
        self.assertEqual(self.project.end_date, LATER)

    def test_a_grant_never_gives_an_open_ended_project_an_end_date(self):
        run = self._submitted_and_assessed()
        self.project.end_date = None
        self.project.save(update_fields=["end_date"])

        self._complete(
            self.call_manager,
            self._active(run),
            WorkflowStepOutcomes.APPROVED,
            expected=status.HTTP_400_BAD_REQUEST,
        )

        self.project.refresh_from_db()
        self.assertIsNone(self.project.end_date)

    def test_an_open_ended_project_is_not_asked_an_end_date(self):
        self.project.end_date = None
        self.project.save(update_fields=["end_date"])

        response = self._post(
            self.applicant,
            "request_extension",
            {"requested_end_date": "2030-12-31"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_resource_ending_later_than_the_grant_keeps_its_end(self):
        self.resource.end_date = LATER
        self.resource.save(update_fields=["end_date"])

        self._extend()

        self.resource.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(self.resource.end_date, LATER)
        self.assertEqual(self.project.end_date, datetime.date(2030, 12, 31))

    def test_a_role_granting_nothing_on_the_offering_does_not_accept_for_it(self):
        offering_ct = ContentType.objects.get_for_model(marketplace_models.Offering)
        nominal = Role.objects.create(
            name="Technical reviewer", content_type=offering_ct
        )
        named = structure_factories.UserFactory()
        self.offering.add_user(named, nominal)
        run = self._request_extension(
            {
                "requested_end_date": "2030-12-31",
                "requested_limits": {self.resource.uuid.hex: {"cpu": 200}},
            }
        )
        self._complete(
            self.applicant, self._active(run), WorkflowStepOutcomes.SUBMITTED
        )
        instance = self._active(run)
        for contribution in instance.contributions.all():
            contributions.submit(
                contribution, named, outcome=ContributionOutcomes.ACCEPTED
            )
        workflow_service.complete_from_contributions(self.proposal, instance, named)
        self._complete(
            self.call_manager, self._active(run), WorkflowStepOutcomes.APPROVED
        )

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, TODAY + datetime.timedelta(days=90))
        self.assertFalse(
            marketplace_models.Order.objects.filter(
                resource=self.resource, type=OrderTypes.UPDATE
            ).exists()
        )

    def test_the_applicant_never_reads_the_grant_of_a_declined_extension(self):
        run = self._submitted_and_assessed()
        response = self._patch_run(
            self.call_manager, run, {"granted_end_date": "2030-11-30"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self._complete(
            self.call_manager, self._active(run), WorkflowStepOutcomes.DECLINED
        )
        run.refresh_from_db()
        self.assertEqual(run.state, ProposalRunStates.REJECTED)

        self.client.force_authenticate(self.applicant)
        response = self.client.get(self._url("runs"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data[0]["granted_end_date"])
        self.assertEqual(response.data[0]["granted_limits"], {})

        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._url("runs"))
        self.assertEqual(str(response.data[0]["granted_end_date"]), "2030-11-30")
