from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.utils import get_system_robot
from waldur_core.permissions.fixtures import ProposalRole
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    OrderStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models, utils
from waldur_mastermind.proposal.enums import (
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures

PDF_BODY = b"%PDF-1.4 fake"


def _pdf(name="po.pdf"):
    return SimpleUploadedFile(name, PDF_BODY, content_type="application/pdf")


class RequirementDerivationTest(test.APITestCase):
    """The call entry seeds its requirement from the offering, then owns it."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    def test_requirement_is_seeded_from_the_offering(self):
        offering = self.fixture.offering
        offering.plugin_options = {"require_purchase_order_upload": True}
        offering.save()

        requested_offering = factories.RequestedOfferingFactory(
            call=self.fixture.call, offering=offering
        )

        self.assertTrue(requested_offering.require_purchase_order)

    def test_requirement_is_off_when_the_offering_does_not_ask_for_one(self):
        requested_offering = factories.RequestedOfferingFactory(
            call=self.fixture.call, offering=self.fixture.offering
        )

        self.assertFalse(requested_offering.require_purchase_order)

    def test_later_offering_changes_do_not_rewrite_the_call_setting(self):
        # The call manager owns the flag once the offering is in the call: an
        # offering toggling its own approval gate must not silently start or
        # stop blocking submissions to a call that already decided.
        requested_offering = factories.RequestedOfferingFactory(
            call=self.fixture.call, offering=self.fixture.offering
        )
        self.fixture.offering.plugin_options = {"require_purchase_order_upload": True}
        self.fixture.offering.save()

        requested_offering.refresh_from_db()
        requested_offering.save()
        requested_offering.refresh_from_db()

        self.assertFalse(requested_offering.require_purchase_order)


class SubmitValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        # Exercise the no-workflow path, as ActionTest does.
        models.CallWorkflowStep.objects.filter(call=self.proposal.round.call).delete()
        self.requested_offering = factories.RequestedOfferingFactory(
            call=self.fixture.call, offering=self.fixture.offering
        )
        self.requested_resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=self.requested_offering,
            resource=None,
        )
        self.url = factories.ProposalFactory.get_url(self.proposal, "submit")
        self.client.force_authenticate(self.fixture.staff)

    def _require_purchase_order(self):
        self.requested_offering.require_purchase_order = True
        self.requested_offering.save()

    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_user_about_proposal_state_update.delay"
    )
    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_call_managers_about_new_proposal_submission.delay"
    )
    def test_submit_is_allowed_when_no_purchase_order_is_required(self, *mocks):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_submit_is_blocked_when_the_purchase_order_is_missing(self):
        self._require_purchase_order()

        response = self.client.post(self.url)

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn(self.fixture.offering.name, str(response.data))
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)

    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_user_about_proposal_state_update.delay"
    )
    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_call_managers_about_new_proposal_submission.delay"
    )
    def test_a_reference_alone_satisfies_the_requirement(self, *mocks):
        self._require_purchase_order()
        self.requested_resource.purchase_order_reference = "PO-4711"
        self.requested_resource.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_user_about_proposal_state_update.delay"
    )
    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_call_managers_about_new_proposal_submission.delay"
    )
    def test_an_attachment_alone_satisfies_the_requirement(self, *mocks):
        self._require_purchase_order()
        self.requested_resource.attachment = _pdf()
        self.requested_resource.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class RequestedAmountsValidationTest(test.APITestCase):
    """A request naming no amount must not pass submission."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        models.CallWorkflowStep.objects.filter(call=self.proposal.round.call).delete()
        # ProposalFixture attaches a requested resource of its own, with no
        # limits. Once the offering gains a requestable component that row is
        # subject to the same rule, so drop it and let each test own the set.
        self.proposal.requestedresource_set.all().delete()
        self.offering = self.fixture.offering
        # A clean slate: the fixture's offering may carry components of its own,
        # and this suite is about whether an amount was named for the ones you
        # can actually ask for.
        self.offering.components.all().delete()
        self.component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu_hours",
            billing_type=BillingTypes.LIMIT,
        )
        self.requested_resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=factories.RequestedOfferingFactory(
                call=self.fixture.call, offering=self.offering
            ),
            resource=None,
            limits={},
        )
        self.url = factories.ProposalFactory.get_url(self.proposal, "submit")
        self.client.force_authenticate(self.fixture.staff)

    def test_submit_is_blocked_when_no_amount_is_named(self):
        response = self.client.post(self.url)

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertIn(self.offering.name, str(response.data))

    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_user_about_proposal_state_update.delay"
    )
    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_call_managers_about_new_proposal_submission.delay"
    )
    def test_submit_is_allowed_once_an_amount_is_named(self, *mocks):
        self.requested_resource.limits = {"cpu_hours": 1000}
        self.requested_resource.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_user_about_proposal_state_update.delay"
    )
    @mock.patch(
        "waldur_mastermind.proposal.views.tasks.notify_call_managers_about_new_proposal_submission.delay"
    )
    def test_an_offering_with_nothing_requestable_is_exempt(self, *mocks):
        # No limit or prepaid component: there is no amount to name.
        self.offering.components.all().delete()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class PurchaseOrderUploadTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        self.requested_resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=factories.RequestedOfferingFactory(
                call=self.fixture.call, offering=self.fixture.offering
            ),
            resource=None,
        )
        self.url = (
            factories.ProposalFactory.get_url(self.proposal, "resources")
            + self.requested_resource.uuid.hex
            + "/purchase_order/"
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_reference_and_document_can_be_uploaded(self):
        response = self.client.post(
            self.url,
            {"attachment": _pdf(), "purchase_order_reference": "PO-4711"},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.requested_resource.refresh_from_db()
        self.assertTrue(self.requested_resource.attachment)
        self.assertEqual(self.requested_resource.purchase_order_reference, "PO-4711")
        self.assertTrue(self.requested_resource.has_purchase_order)

    def test_purchase_order_can_be_removed(self):
        self.requested_resource.attachment = _pdf()
        self.requested_resource.purchase_order_reference = "PO-4711"
        self.requested_resource.save()

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.requested_resource.refresh_from_db()
        self.assertFalse(self.requested_resource.attachment)
        self.assertEqual(self.requested_resource.purchase_order_reference, "")

    def test_upload_is_rejected_once_the_proposal_leaves_draft(self):
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()

        response = self.client.post(
            self.url, {"purchase_order_reference": "PO-4711"}, format="multipart"
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)


class AllocationCarryThroughTest(test.APITestCase):
    """The purchase order must reach the order, not be asked for twice."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.project = None
        self.proposal.save()
        self.requested_resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=factories.RequestedOfferingFactory(
                call=self.fixture.call, offering=self.fixture.offering
            ),
            resource=None,
        )

    def _allocated_order(self):
        self.requested_resource.refresh_from_db()
        return marketplace_models.Order.objects.get(
            resource=self.requested_resource.resource
        )

    def test_reference_and_document_are_copied_onto_the_order(self):
        self.requested_resource.attachment = _pdf()
        self.requested_resource.purchase_order_reference = "PO-4711"
        self.requested_resource.save()

        utils.allocate_proposal(self.proposal)

        order = self._allocated_order()
        # Name equality is the assertion that matters: allocate_proposal copies
        # the stored path rather than the file, so the order must point at the
        # very object the applicant uploaded. Truthiness alone would still pass
        # if the order pointed somewhere else, or at a path with no blob behind
        # it — hence the read-back through the order's own field.
        self.assertEqual(order.attachment.name, self.requested_resource.attachment.name)
        with order.attachment.open("rb") as document:
            self.assertEqual(document.read(), PDF_BODY)
        self.assertEqual(order.request_comment, "PO-4711")

    def test_nothing_is_copied_when_no_purchase_order_was_given(self):
        utils.allocate_proposal(self.proposal)

        order = self._allocated_order()
        self.assertFalse(order.attachment)
        self.assertFalse(order.request_comment)

    def test_order_is_consumer_approved_on_allocation(self):
        # The call review already authorised the spend, so nothing is left for
        # the consumer to decide. Left pending, the order both parks the
        # resource in CREATING and re-asks for the purchase order the proposal
        # already collected.
        utils.allocate_proposal(self.proposal)

        order = self._allocated_order()
        self.assertNotEqual(order.state, OrderStates.PENDING_CONSUMER)
        self.assertEqual(order.consumer_reviewed_by, get_system_robot())
        self.assertIsNotNone(order.consumer_reviewed_at)

    def test_provider_review_still_applies(self):
        # Only the consumer step is skipped. An offering whose provider reviews
        # orders must still get its say.
        self.fixture.offering.type = BASIC_OFFERING
        self.fixture.offering.save()

        utils.allocate_proposal(self.proposal)

        self.assertEqual(self._allocated_order().state, OrderStates.PENDING_PROVIDER)


class AllocationCarryThroughEndToEndTest(test.APITestCase):
    """The document survives the whole path the applicant actually walks.

    The tests above assign the file to the model and call allocate_proposal
    directly, which leaves the two ends untested: the multipart upload the form
    performs, and the workflow terminal that triggers allocation.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.proposal.project = None
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "allocation_decision"
        self.proposal.save()
        self.requested_resource = factories.RequestedResourceFactory(
            proposal=self.proposal,
            requested_offering=factories.RequestedOfferingFactory(
                call=self.fixture.call, offering=self.fixture.offering
            ),
            resource=None,
        )
        factories.CallWorkflowStepFactory(
            call=self.fixture.call, step="allocation_decision"
        )
        self.step_instance = models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="allocation_decision",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_uploaded_document_reaches_the_order_through_the_workflow(self):
        upload_url = (
            factories.ProposalFactory.get_url(self.proposal, "resources")
            + self.requested_resource.uuid.hex
            + "/purchase_order/"
        )
        # The proposal has to be in draft to accept the upload, exactly as the
        # applicant's own sequence goes: attach, then submit, then get reviewed.
        self.proposal.state = ProposalStates.DRAFT
        self.proposal.save()
        upload_response = self.client.post(
            upload_url,
            {"attachment": _pdf(), "purchase_order_reference": "PO-4711"},
            format="multipart",
        )
        self.assertEqual(
            upload_response.status_code, status.HTTP_200_OK, upload_response.data
        )
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()

        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="complete_workflow_step"
            ),
            {"step_uuid": self.step_instance.uuid.hex, "outcome": "approved"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.requested_resource.refresh_from_db()
        order = marketplace_models.Order.objects.get(
            resource=self.requested_resource.resource
        )
        self.assertEqual(order.attachment.name, self.requested_resource.attachment.name)
        with order.attachment.open("rb") as document:
            self.assertEqual(document.read(), PDF_BODY)
        self.assertEqual(order.request_comment, "PO-4711")
        self.assertNotEqual(order.state, OrderStates.PENDING_CONSUMER)
