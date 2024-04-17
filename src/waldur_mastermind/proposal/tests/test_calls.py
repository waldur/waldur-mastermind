from ddt import data, ddt
from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class PublicCallGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    @data(
        "staff",
        "owner",
        "user",
        "customer_support",
    )
    def test_active_call_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_active_call_should_be_visible_to_unauthenticated_users(
        self,
    ):
        url = factories.CallFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)


@ddt
class CallGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_call_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    @data("user")
    def test_call_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)


@ddt
class CallCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.manager = self.fixture.manager

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_create_call(self, user):
        response = self.create_call(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Call.objects.filter(uuid=response.data["uuid"]).exists())

    @data("user")
    def test_user_can_not_create_call(self, user):
        response = self.create_call(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def create_call(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_list_url()

        payload = {
            "name": "new call",
            "manager": factories.CallManagingOrganisationFactory.get_url(self.manager),
        }

        return self.client.post(url, payload)


@ddt
class CallUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.manager = self.fixture.manager

    @data("staff", "owner", "customer_support")
    def test_user_can_update_call(self, user):
        response = self.update_call(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.call.description, "new description")

    @data("user")
    def test_user_can_not_update_call(self, user):
        response = self.update_call(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def update_call(self, user, payload=None, **kwargs):
        if not payload:
            payload = {"description": "new description"}

        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(url, payload, **kwargs)
        self.call.refresh_from_db()
        return response

    def _upload_call_document(self):
        url = factories.CallFactory.get_protected_url(
            self.call, action="attach_documents"
        )
        payload = {
            "documents": [
                {"file": dummy_image()},
                {"file": dummy_image()},
            ],
        }
        return self.client.post(url, payload, format="multipart")

    @data("staff", "owner", "customer_support")
    def test_upload_documents(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self._upload_call_document()
        call = models.Call.objects.get(uuid=self.call.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(call.calldocument_set.all()), 2)

    @data("staff", "owner", "customer_support")
    def test_remove_documents(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(
            self.call, action="detach_documents"
        )
        self._upload_call_document()
        call_document_for_removal = models.CallDocument.objects.last()
        payload = {
            "documents": [call_document_for_removal.uuid],
        }
        response = self.client.post(url, payload, format="multipart")
        call = models.Call.objects.get(uuid=self.call.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(call.documents.all()), 1)


@ddt
class CallDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft_call = self.fixture.new_call
        self.active_call = self.fixture.call

    @data("staff", "owner", "customer_support")
    def test_user_can_delete_call(self, user):
        response = self.delete_call(user, self.draft_call)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.Call.objects.filter(uuid=self.draft_call.uuid.hex).exists()
        )

    @data("staff", "owner", "customer_support")
    def test_user_can_not_delete_active_call(self, user):
        response = self.delete_call(user, self.active_call)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertTrue(
            models.Call.objects.filter(uuid=self.active_call.uuid.hex).exists()
        )

    @data("user")
    def test_user_can_not_delete_call(self, user):
        response = self.delete_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            models.Call.objects.filter(uuid=self.draft_call.uuid.hex).exists()
        )

    def delete_call(self, user, call):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(call)
        response = self.client.delete(url)
        return response


@ddt
class CallActivateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft_call = self.fixture.new_call
        self.active_call = self.fixture.call

    @data("staff", "owner", "customer_support")
    def test_user_can_activate_call_with_round(self, user):
        factories.RoundFactory(
            call=self.draft_call,
        )
        response = self.activate_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.draft_call.state, models.Call.States.ACTIVE)

    @data("staff")
    def test_user_can_not_activate_call_without_round(self, user):
        response = self.activate_call(user, self.draft_call)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertEqual(self.draft_call.state, models.Call.States.DRAFT)

    @data("staff", "owner", "customer_support")
    def test_user_can_not_activate_active_call(self, user):
        response = self.activate_call(user, self.active_call)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(self.active_call.state, models.Call.States.ACTIVE)

    @data("user")
    def test_user_can_not_activate_call(self, user):
        response = self.activate_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)
        self.assertEqual(self.active_call.state, models.Call.States.ACTIVE)

    def activate_call(self, user, call):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(call, "activate")
        response = self.client.post(url)
        call.refresh_from_db()
        return response


@ddt
class CallArchiveTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft_call = self.fixture.new_call

    @data("staff", "owner", "customer_support")
    def test_user_can_archive_call(self, user):
        response = self.archive_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.draft_call.state, models.Call.States.ARCHIVED)

    @data("user")
    def test_user_can_not_archive_call(self, user):
        response = self.archive_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)
        self.assertEqual(self.draft_call.state, models.Call.States.DRAFT)

    def archive_call(self, user, call):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(call, "archive")
        response = self.client.post(url)
        call.refresh_from_db()
        return response


@ddt
class RequestedOfferingsGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedOfferingFactory.get_list_url(self.fixture.call)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_call_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("user")
    def test_call_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class RequestedOfferingsCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedOfferingFactory.get_list_url(self.fixture.call)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_add_offering_to_call(self, user):
        response = self.add_offering(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.RequestedOffering.objects.filter(uuid=response.data["uuid"]).exists()
        )

    @data("user")
    def test_user_can_not_add_offering_to_call(self, user):
        response = self.add_offering(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_validate_attributes(self):
        user = getattr(self.fixture, "staff")
        self.client.force_authenticate(user)

        payload = {
            "offering": marketplace_factories.OfferingFactory.get_public_url(
                self.fixture.offering
            ),
            "attributes": None,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_attributes_are_not_required(self):
        user = getattr(self.fixture, "staff")
        self.client.force_authenticate(user)

        payload = {
            "offering": marketplace_factories.OfferingFactory.get_public_url(
                self.fixture.offering
            )
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def add_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "offering": marketplace_factories.OfferingFactory.get_public_url(
                self.fixture.offering
            ),
            "attributes": '{"cores": 100}',
        }

        return self.client.post(self.url, payload)


@ddt
class RequestedOfferingsUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_offering = self.fixture.requested_offering
        self.url = factories.RequestedOfferingFactory.get_url(
            self.fixture.call, self.requested_offering
        )

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_update_requested_offering(self, user):
        response = self.update_requested_offering(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_not_update_accepted_offering(self, user):
        self.requested_offering.state = models.RequestedOffering.States.ACCEPTED
        self.requested_offering.save()
        response = self.update_requested_offering(user)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data("user")
    def test_user_can_not_add_offering_to_call(self, user):
        response = self.update_requested_offering(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def update_requested_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "attributes": '{"cores": 300}',
        }
        response = self.client.patch(self.url, payload)
        self.requested_offering.refresh_from_db()
        return response


@ddt
class RequestedOfferingsDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_offering = self.fixture.requested_offering
        self.url = factories.RequestedOfferingFactory.get_url(
            self.fixture.call, self.requested_offering
        )

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_update_requested_offering(self, user):
        response = self.delete_requested_offering(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("user")
    def test_user_can_not_add_offering_to_call(self, user):
        response = self.delete_requested_offering(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_requested_offering_with_connected_proposals(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.RequestedOfferingFactory.get_url(
            self.fixture.call, self.fixture.requested_offering_accepted
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def delete_requested_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)
