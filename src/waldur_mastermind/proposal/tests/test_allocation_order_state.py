"""How the order behind an allocated item went, as the proposal reads it."""

from rest_framework import status

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import ProjectRole
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_awarded_resources import (
    AwardedResourcesBase,
)


class AllocationOrderStateTest(AwardedResourcesBase):
    def setUp(self):
        super().setUp()
        # permissions.yaml grants ORDER.LIST to project managers; tests do not
        # import that file, so mirror the deployment here.
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        self._start_allocation()

    def _allocate(self):
        self._decide()
        self.order = marketplace_models.Order.objects.get(project=self.proposal.project)

    def _set_order(self, state, comment=""):
        marketplace_models.Order.objects.filter(pk=self.order.pk).update(
            state=state, provider_rejection_comment=comment
        )

    def _awarded_row(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        (row,) = response.data
        return row

    def _requested_row(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="resources")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        (row,) = response.data
        return row

    def test_nothing_is_reported_before_allocation(self):
        row = self._awarded_row(self.call_manager)

        self.assertIsNone(row["order_state"])
        self.assertIsNone(row["order_rejection_comment"])
        self.assertIsNone(self._requested_row(self.call_manager)["order_state"])

    def test_the_applicant_reads_the_order_state_on_both_rows(self):
        self._allocate()
        for state, name in (
            (OrderStates.PENDING_PROVIDER, "pending-provider"),
            (OrderStates.EXECUTING, "executing"),
            (OrderStates.ERRED, "erred"),
            (OrderStates.DONE, "done"),
        ):
            self._set_order(state)
            self.assertEqual(self._awarded_row(self.applicant)["order_state"], name)
            self.assertEqual(self._requested_row(self.applicant)["order_state"], name)

    def test_the_refusal_reason_is_shown_to_whoever_may_read_the_order(self):
        self._allocate()
        self._set_order(OrderStates.REJECTED, "No capacity this quarter")
        self.proposal.project.add_user(self.applicant, ProjectRole.MANAGER)

        awarded = self._awarded_row(self.applicant)
        requested = self._requested_row(self.applicant)

        self.assertEqual(awarded["order_state"], "rejected")
        self.assertEqual(awarded["order_rejection_comment"], "No capacity this quarter")
        self.assertEqual(
            requested["order_rejection_comment"], "No capacity this quarter"
        )

    def test_the_refusal_reason_is_withheld_from_a_viewer_without_the_order(self):
        self._allocate()
        self._set_order(OrderStates.REJECTED, "Internal capacity note")

        # The award settles the applicant team on the granted project; here
        # the applicant has since left it and holds no role there.
        self.proposal.refresh_from_db()
        self.proposal.project.remove_user(self.applicant)
        awarded = self._awarded_row(self.applicant)
        requested = self._requested_row(self.applicant)

        self.assertEqual(awarded["order_state"], "rejected")
        self.assertIsNone(awarded["order_rejection_comment"])
        self.assertEqual(requested["order_state"], "rejected")
        self.assertIsNone(requested["order_rejection_comment"])

    def test_no_reason_is_reported_for_an_order_that_was_not_refused(self):
        self._allocate()
        self._set_order(OrderStates.DONE, "left over")
        self.proposal.project.add_user(self.applicant, ProjectRole.MANAGER)

        self.assertIsNone(self._awarded_row(self.applicant)["order_rejection_comment"])
