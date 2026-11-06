"""A call that lets the applicant choose the project's length from a list.

The applicant picks one of the call's ``allowed_duration_months`` before
submitting; the award starts from that choice and the call manager may move
it to another listed length while the allocation decision is open. The
project runs for the awarded length, and no subscription in it may outlast
it.
"""

import datetime

from dateutil.relativedelta import relativedelta
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import BillingTypes, LimitPeriods
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import call_templates, models, utils
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_awarded_resources import (
    AwardedResourcesBase,
)

PREPAID_FIELD = "attributes.prepaid_duration_months"


def _call_url(call):
    return f"/api/proposal-protected-calls/{call.uuid.hex}/"


class CallDurationPolicyTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def _patch(self, **payload):
        return self.client.patch(_call_url(self.call), payload, format="json")

    def test_the_list_is_stored_distinct_and_sorted(self):
        response = self._patch(allowed_duration_months=[12, 3, 6, 6])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertEqual(self.call.allowed_duration_months, [3, 6, 12])
        self.assertEqual(response.data["allowed_duration_months"], [3, 6, 12])

    def test_a_list_and_a_fixed_duration_are_exclusive(self):
        response = self._patch(
            allowed_duration_months=[3, 6], fixed_duration_in_days=365
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_duration_months", response.data)

    def test_a_list_is_refused_on_a_call_that_fixes_the_duration(self):
        self.call.fixed_duration_in_days = 365
        self.call.save()

        response = self._patch(allowed_duration_months=[3, 6])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_duration_months", response.data)

    def test_a_fixed_duration_is_refused_on_a_call_with_a_list(self):
        self.call.allowed_duration_months = [3, 6]
        self.call.save()

        response = self._patch(fixed_duration_in_days=365)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("fixed_duration_in_days", response.data)

    def test_switching_from_fixed_to_a_list_in_one_request(self):
        self.call.fixed_duration_in_days = 365
        self.call.save()

        response = self._patch(fixed_duration_in_days=None, allowed_duration_months=[6])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_length_below_one_month_is_refused(self):
        response = self._patch(allowed_duration_months=[0, 6])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_duration_months", response.data)

    def test_applicants_read_the_list_on_the_public_call(self):
        self.call.allowed_duration_months = [3, 6]
        self.call.save()
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.get(f"/api/proposal-public-calls/{self.call.uuid.hex}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["allowed_duration_months"], [3, 6])

    def test_a_duplicated_call_keeps_the_list(self):
        self.call.allowed_duration_months = [3, 6, 12]
        self.call.save()

        copy = utils.duplicate_call(self.call, "Copy", self.staff)

        self.assertEqual(copy.allowed_duration_months, [3, 6, 12])

    def test_the_model_refuses_both_policies(self):
        self.call.allowed_duration_months = [3]
        self.call.fixed_duration_in_days = 30

        with self.assertRaises(Exception) as cm:
            self.call.clean()

        self.assertIn("allowed_duration_months", str(cm.exception.detail))


class ApplicantChoiceTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.allowed_duration_months = [3, 6, 12]
        self.call.save()
        self.proposal = self.fixture.proposal
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check", duration_in_days=5
        )
        factories.CallWorkflowStepFactory(
            call=self.call, step="allocation_decision", duration_in_days=10
        )
        self.client.force_authenticate(self.proposal.created_by)

    def _choose(self, months):
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="update_project_details"
            ),
            {"name": self.proposal.name, "requested_duration_months": months},
            format="json",
        )

    def _submit(self):
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="submit")
        )

    def test_the_applicant_chooses_a_listed_length(self):
        response = self._choose(6)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.requested_duration_months, 6)

    def test_an_unlisted_length_is_refused(self):
        response = self._choose(4)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requested_duration_months", response.data)

    def test_a_call_without_a_list_refuses_a_choice(self):
        self.call.allowed_duration_months = []
        self.call.save()

        response = self._choose(6)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requested_duration_months", response.data)

    def test_submission_without_a_length_is_refused(self):
        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requested_duration_months", response.data)
        self.assertFalse(self.proposal.can_submit()[0])

    def test_submission_with_a_length_no_longer_listed_is_refused(self):
        self.proposal.requested_duration_months = 4
        self.proposal.save()

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requested_duration_months", response.data)

    def test_submission_with_a_listed_length_goes_through(self):
        self._choose(6)

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.requested_duration_months, 6)

    def test_a_call_without_a_list_ignores_the_choice_at_submission(self):
        self.proposal.requested_duration_months = 6
        self.proposal.save()
        self.call.allowed_duration_months = []
        self.call.save()

        response = self._submit()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertIsNone(self.proposal.requested_duration_months)

    def test_the_length_cannot_change_after_submission(self):
        self._choose(6)
        self._submit()

        response = self._choose(3)

        self.assertNotEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.requested_duration_months, 6)


class PrepaidCapTest(test.APITestCase):
    """No subscription may outlast the length the applicant chose."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.allowed_duration_months = [3, 6, 12]
        self.call.save()
        self.proposal = self.fixture.proposal
        self.proposal.requested_duration_months = 6
        self.proposal.save()
        offering = self.fixture.offering
        offering.components.all().delete()
        self.cpu = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="cpu_hours",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
            limit_period=LimitPeriods.MONTH,
        )
        plan = marketplace_factories.PlanFactory(offering=offering, unit="month")
        marketplace_factories.PlanComponentFactory(
            plan=plan, component=self.cpu, price=1, amount=0
        )
        self.requested_offering = factories.RequestedOfferingFactory(
            call=self.call, offering=offering, plan=plan
        )
        self.client.force_authenticate(self.fixture.proposal_creator)

    def _request(self, months):
        return self.client.post(
            factories.RequestedResourceFactory.get_list_url(self.proposal),
            {
                "requested_offering_uuid": self.requested_offering.uuid.hex,
                "attributes": {"prepaid_duration_months": months},
                "limits": {"cpu_hours": 10},
            },
            format="json",
        )

    def test_a_subscription_within_the_chosen_length_is_accepted(self):
        response = self._request(6)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_subscription_longer_than_the_chosen_length_is_refused(self):
        response = self._request(12)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(PREPAID_FIELD, response.data)

    def test_shortening_the_project_below_a_requested_subscription_is_refused(self):
        self.assertEqual(self._request(6).status_code, status.HTTP_201_CREATED)

        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="update_project_details"
            ),
            {"name": self.proposal.name, "requested_duration_months": 3},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requested_duration_months", response.data)


class AwardedDurationTest(AwardedResourcesBase):
    def setUp(self):
        super().setUp()
        self.call.allowed_duration_months = [3, 6, 12]
        self.call.save()
        self.proposal.requested_duration_months = 6
        self.proposal.save()

    def _set(self, months, user=None):
        self.client.force_authenticate(user or self.call_manager)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="set_awarded_duration"
            ),
            {"awarded_duration_months": months},
            format="json",
        )

    def _read(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_the_award_starts_from_the_requested_length(self):
        self._start_allocation()

        self.assertEqual(self.proposal.awarded_duration_months, 6)

    def test_the_project_runs_for_the_requested_length(self):
        self._start_allocation()

        self._decide()

        project = self.proposal.project
        self.assertEqual(
            project.end_date, datetime.date.today() + relativedelta(months=6)
        )

    def test_the_call_manager_awards_another_listed_length(self):
        self._start_allocation()

        response = self._set(3)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self._decide()
        self.assertEqual(self.proposal.awarded_duration_months, 3)
        self.assertEqual(
            self.proposal.project.end_date,
            datetime.date.today() + relativedelta(months=3),
        )
        # The applicant's request stays as it was submitted.
        self.assertEqual(self.proposal.requested_duration_months, 6)

    def test_an_unlisted_award_is_refused(self):
        self._start_allocation()

        response = self._set(4)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("awarded_duration_months", response.data)

    def test_the_applicant_cannot_set_the_award(self):
        self._start_allocation()

        response = self._set(3, user=self.applicant)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_call_manager_on_the_team_does_not_set_their_own_award(self):
        self._start_allocation()
        self.proposal.add_user(self.call_manager, ProposalRole.MANAGER)

        response = self._set(3)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.awarded_duration_months, 6)

    def test_the_award_is_closed_outside_the_allocation_decision(self):
        response = self._set(3)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_award_is_closed_after_the_decision(self):
        self._start_allocation()
        self._decide()

        response = self._set(3)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_applicant_reads_the_awarded_length_only_once_released(self):
        self._start_allocation()
        self._set(3)

        self.assertIsNone(self._read(self.applicant)["awarded_duration_months"])
        self.assertEqual(self._read(self.applicant)["requested_duration_months"], 6)
        self.assertEqual(self._read(self.call_manager)["awarded_duration_months"], 3)

        self._decide()

        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual(self._read(self.applicant)["awarded_duration_months"], 3)

    def test_a_reviewer_does_not_read_the_awarded_length(self):
        self._start_allocation()
        factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.fixture.reviewer_1
        )

        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["awarded_duration_months"])

    def test_without_a_list_the_project_length_is_unchanged(self):
        self.call.allowed_duration_months = []
        self.call.save()
        self.proposal.requested_duration_months = None
        self.proposal.save()
        self._start_allocation()

        self._decide()

        self.assertIsNone(self.proposal.awarded_duration_months)
        self.assertIsNone(self.proposal.project.end_date)


class ChosenLengthPrecedenceTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.call = self.fixture.call
        self.start = datetime.date(2026, 1, 31)

    def test_a_fixed_duration_wins(self):
        self.call.fixed_duration_in_days = 100
        self.call.save()
        self.proposal.requested_duration_months = 6
        self.proposal.save()

        self.assertEqual(
            utils.project_end_date(self.proposal, self.start),
            self.start + datetime.timedelta(days=100),
        )

    def test_the_chosen_months_come_next(self):
        self.proposal.requested_duration_months = 1

        self.assertEqual(
            utils.project_end_date(self.proposal, self.start),
            datetime.date(2026, 2, 28),
        )

    def test_the_awarded_months_replace_the_requested_once_prefilled(self):
        self.proposal.requested_duration_months = 6
        self.proposal.awarded_duration_months = 3
        self.proposal.awarded_resources_prefilled_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.UTC
        )

        self.assertEqual(
            utils.project_end_date(self.proposal, self.start),
            self.start + relativedelta(months=3),
        )

    def test_the_label_names_the_requested_months(self):
        self.proposal.requested_duration_months = 6

        self.assertEqual(utils.requested_duration_label(self.proposal), "6 months")


class TemplatesOfferChoicesTest(test.APITestCase):
    def test_the_ai_factory_and_quantum_templates_offer_lengths(self):
        staff = structure_factories.UserFactory(is_staff=True)
        manager = factories.CallManagingOrganisationFactory()
        expected = {"ai_factory_large_scale": [3, 6, 12], "quantum_access": [3, 6]}
        self.client.force_authenticate(staff)
        for template, months in expected.items():
            with self.subTest(template=template):
                response = self.client.post(
                    factories.CallFactory.get_protected_list_url("import_call"),
                    {
                        "manager": manager.uuid.hex,
                        "call_data": call_templates.load(template),
                    },
                    format="json",
                )
                self.assertEqual(
                    response.status_code, status.HTTP_201_CREATED, response.data
                )
                call = models.Call.objects.get(uuid=response.data["call_uuid"])
                self.assertEqual(call.allowed_duration_months, months)
                self.assertIsNone(call.fixed_duration_in_days)
