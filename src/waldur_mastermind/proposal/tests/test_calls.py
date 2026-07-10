import uuid

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.checklist.tests import factories as checklist_factories
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import CallStates, RequestedOfferingStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class PublicCallGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    @data(
        "staff",
        "owner",
        "user",
        "customer_support",
        "call_manager",
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
class CallGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    def test_staff_can_get_all_calls(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)

    def test_call_manager_can_get_related_calls(self):
        user = self.fixture.call_manager
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # In the fixture, call_manager added only to self.call
        self.assertEqual(len(response.json()), 1)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_call_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)


@ddt
class CallCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.manager = self.fixture.manager

    @data(
        "staff",
        "call_organizer_user",
    )
    def test_user_can_create_call(self, user):
        response = self.create_call(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Call.objects.filter(uuid=response.data["uuid"]).exists())

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_create_call(self, user):
        response = self.create_call(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(
        "call_manager",
    )
    def test_call_manager_can_not_create_call(self, user):
        response = self.create_call(user)
        # fails with 400 because call_manager has no access to call managing organization
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.manager.add_user(self.fixture.call_manager, CallRole.MANAGER)
        response = self.create_call(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_update_call(self, user):
        response = self.update_call(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.call.description, "new description")

    @data(
        "user",
        "owner",
        "customer_support",
    )
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

    @data(
        "staff",
        "call_manager",
    )
    def test_upload_documents(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self._upload_call_document()
        call = models.Call.objects.get(uuid=self.call.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(call.calldocument_set.all()), 2)

    @data(
        "staff",
        "call_manager",
    )
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
class CallDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft_call = self.fixture.new_call
        self.active_call = self.fixture.call
        self.draft_call.add_user(self.fixture.call_manager, CallRole.MANAGER)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_delete_call(self, user):
        response = self.delete_call(user, self.draft_call)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.Call.objects.filter(uuid=self.draft_call.uuid.hex).exists()
        )

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_not_delete_active_call(self, user):
        response = self.delete_call(user, self.active_call)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertTrue(
            models.Call.objects.filter(uuid=self.active_call.uuid.hex).exists()
        )

    @data(
        "user",
        "owner",
        "customer_support",
    )
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
class CallActivateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft_call = self.fixture.new_call
        self.active_call = self.fixture.call
        self.draft_call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        # A call must have at least one offering to be activated.
        factories.RequestedOfferingFactory(call=self.draft_call)

    def test_user_can_not_activate_call_without_offering(self):
        factories.RoundFactory(call=self.draft_call)
        self.draft_call.requestedoffering_set.all().delete()
        response = self.activate_call("staff", self.draft_call)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertEqual(self.draft_call.state, CallStates.DRAFT)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_activate_call_with_round_and_reviewer(self, user):
        factories.RoundFactory(call=self.draft_call)
        self.draft_call.add_user(self.fixture.user, CallRole.REVIEWER)
        response = self.activate_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.draft_call.state, CallStates.ACTIVE)

    @data("staff")
    def test_user_can_not_activate_call_without_round(self, user):
        response = self.activate_call(user, self.draft_call)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertEqual(self.draft_call.state, CallStates.DRAFT)

    @data("staff")
    def test_user_can_activate_call_without_reviewer(self, user):
        factories.RoundFactory(
            call=self.draft_call,
        )
        response = self.activate_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.draft_call.state, CallStates.ACTIVE)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_not_activate_active_call(self, user):
        response = self.activate_call(user, self.active_call)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)
        self.assertEqual(self.active_call.state, CallStates.ACTIVE)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_activate_call(self, user):
        response = self.activate_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)
        self.assertEqual(self.active_call.state, CallStates.ACTIVE)

    def activate_call(self, user, call):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(call, "activate")
        response = self.client.post(url)
        call.refresh_from_db()
        return response


@ddt
class CallArchiveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.draft_call = self.fixture.new_call
        self.draft_call.add_user(self.fixture.call_manager, CallRole.MANAGER)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_archive_call(self, user):
        response = self.archive_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.draft_call.state, CallStates.ARCHIVED)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_archive_call(self, user):
        response = self.archive_call(user, self.draft_call)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)
        self.assertEqual(self.draft_call.state, CallStates.DRAFT)

    def archive_call(self, user, call):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(call, "archive")
        response = self.client.post(url)
        call.refresh_from_db()
        return response


@ddt
class CallDuplicateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.source = self.fixture.call
        self.source.add_user(self.fixture.call_manager, CallRole.MANAGER)
        # Seed config that should follow the duplicate.
        factories.RoundFactory(call=self.source)
        factories.RequestedOfferingFactory(call=self.source)

    @data("staff", "call_organizer_user")
    def test_user_can_duplicate_call(self, user):
        response = self.duplicate_call(user, self.source, name="Copy of call")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_uuid = response.data["uuid"]
        self.assertNotEqual(new_uuid, self.source.uuid.hex)
        new_call = models.Call.objects.get(uuid=new_uuid)
        self.assertEqual(new_call.name, "Copy of call")
        self.assertEqual(new_call.state, CallStates.DRAFT)
        # Config travels with the duplicate.
        self.assertEqual(new_call.round_set.count(), self.source.round_set.count())
        self.assertEqual(
            new_call.requestedoffering_set.count(),
            self.source.requestedoffering_set.count(),
        )
        # New offerings start in REQUESTED state regardless of source state.
        self.assertTrue(
            all(
                ro.state == RequestedOfferingStates.REQUESTED
                for ro in new_call.requestedoffering_set.all()
            )
        )
        # Source unchanged.
        self.source.refresh_from_db()
        self.assertEqual(self.source.state, CallStates.ACTIVE)

    def test_call_manager_can_not_duplicate_call(self):
        # CallRole.MANAGER can update an existing call but lacks CREATE_CALL,
        # which is required to spawn a new one.
        response = self.duplicate_call("call_manager", self.source, name="Copy of call")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    @data("user", "owner", "customer_support")
    def test_user_can_not_duplicate_call(self, user):
        response = self.duplicate_call(user, self.source, name="Copy of call")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)

    def test_duplicate_requires_name(self):
        response = self.duplicate_call("staff", self.source, name="")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_duplicate_does_not_copy_proposals(self):
        # Attach a proposal to the source's first round so we can prove it
        # does not appear in the duplicate.
        source_round = self.source.round_set.first()
        factories.ProposalFactory(round=source_round)
        response = self.duplicate_call("staff", self.source, name="Copy")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_call = models.Call.objects.get(uuid=response.data["uuid"])
        proposal_count = sum(r.proposal_set.count() for r in new_call.round_set.all())
        self.assertEqual(proposal_count, 0)

    def test_duplicate_can_skip_sections(self):
        # Skip rounds and offerings on the duplicate; expect zero of each.
        response = self.duplicate_call(
            "staff",
            self.source,
            name="Skinny copy",
            copy_rounds=False,
            copy_offerings=False,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_call = models.Call.objects.get(uuid=response.data["uuid"])
        self.assertEqual(new_call.round_set.count(), 0)
        self.assertEqual(new_call.requestedoffering_set.count(), 0)

    def test_duplicate_skips_resource_templates_when_offerings_excluded(self):
        # Resource templates depend on RequestedOffering; skipping offerings
        # must also drop the templates to avoid orphan FKs.
        ro = self.source.requestedoffering_set.first()
        factories.CallResourceTemplateFactory(call=self.source, requested_offering=ro)
        response = self.duplicate_call(
            "staff",
            self.source,
            name="No-offerings copy",
            copy_offerings=False,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_call = models.Call.objects.get(uuid=response.data["uuid"])
        self.assertEqual(new_call.resource_templates.count(), 0)

    def duplicate_call(self, user, call, *, name, **section_flags):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_protected_url(call, "duplicate")
        return self.client.post(url, {"name": name, **section_flags})


@ddt
class RequestedOfferingsGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedOfferingFactory.get_list_url(self.fixture.call)

    @data(
        "staff",
        "call_manager",
    )
    def test_call_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_call_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_state_filter_applied(self):
        factories.RequestedOfferingFactory(
            call=self.fixture.call,
            state=RequestedOfferingStates.ACCEPTED,
        )
        factories.RequestedOfferingFactory(
            call=self.fixture.call,
            state=RequestedOfferingStates.CANCELED,
        )

        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self.url, {"state": RequestedOfferingStates.REQUESTED}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["state"], RequestedOfferingStates.REQUESTED)

    def test_state_filter_multiple_states(self):
        factories.RequestedOfferingFactory(
            call=self.fixture.call,
            state=RequestedOfferingStates.ACCEPTED,
        )
        factories.RequestedOfferingFactory(
            call=self.fixture.call,
            state=RequestedOfferingStates.CANCELED,
        )

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url,
            {
                "state": [
                    RequestedOfferingStates.REQUESTED,
                    RequestedOfferingStates.ACCEPTED,
                ]
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return 3: 1 REQUESTED from fixture, 1 ACCEPTED from fixture, 1 ACCEPTED created here
        self.assertEqual(len(response.json()), 3)
        states = {item["state"] for item in response.json()}
        self.assertEqual(
            states,
            {RequestedOfferingStates.REQUESTED, RequestedOfferingStates.ACCEPTED},
        )

    def test_state_filter_no_results(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"state": RequestedOfferingStates.CANCELED}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_without_state_filter_returns_all(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return 2 from fixtures
        self.assertEqual(len(response.json()), 2)

    def test_state_filter_ignores_invalid_values(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"state": "invalid_state"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return all offerings from fixture: 2
        self.assertEqual(len(response.json()), 2)

    def test_state_filter_mixed_valid_and_invalid(self):
        factories.RequestedOfferingFactory(
            call=self.fixture.call,
            state=RequestedOfferingStates.ACCEPTED,
        )
        factories.RequestedOfferingFactory(
            call=self.fixture.call,
            state=RequestedOfferingStates.CANCELED,
        )

        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self.url,
            {
                "state": [
                    RequestedOfferingStates.REQUESTED,
                    "invalid_state",
                    RequestedOfferingStates.ACCEPTED,
                ]
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return only REQUESTED and ACCEPTED (ignoring "invalid_state")
        self.assertEqual(len(response.json()), 3)
        states = {item["state"] for item in response.json()}
        self.assertEqual(
            states,
            {RequestedOfferingStates.REQUESTED, RequestedOfferingStates.ACCEPTED},
        )


@ddt
class RequestedOfferingsCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedOfferingFactory.get_list_url(self.fixture.call)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_add_offering_to_call(self, user):
        response = self.add_offering(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.RequestedOffering.objects.filter(uuid=response.data["uuid"]).exists()
        )

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_add_offering_to_call(self, user):
        response = self.add_offering(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_validate_attributes(self):
        user = self.fixture.staff
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
        user = self.fixture.staff
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
class RequestedOfferingsUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_offering = self.fixture.requested_offering
        self.url = factories.RequestedOfferingFactory.get_url(
            self.fixture.call, self.requested_offering
        )

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_update_requested_offering(self, user):
        response = self.update_requested_offering(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_not_update_accepted_offering(self, user):
        self.requested_offering.state = RequestedOfferingStates.ACCEPTED
        self.requested_offering.save()
        response = self.update_requested_offering(user)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data(
        "user",
        "owner",
        "customer_support",
    )
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
class RequestedOfferingsDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_offering = self.fixture.requested_offering
        self.url = factories.RequestedOfferingFactory.get_url(
            self.fixture.call, self.requested_offering
        )

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_update_requested_offering(self, user):
        response = self.delete_requested_offering(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data(
        "user",
        "owner",
        "customer_support",
    )
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

    def test_call_organizer_user_can_get_call_via_proposal_endpoint(self):
        self.client.force_authenticate(self.fixture.call_organizer_user)
        url = factories.CallFactory.get_protected_url(self.fixture.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.fixture.call.uuid.hex)


@ddt
class AvailableChecklistsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.checklist = checklist_factories.ChecklistFactory(
            checklist_type=ChecklistTypes.PROPOSAL_COMPLIANCE
        )
        self.url = factories.CallFactory.get_protected_list_url(
            action="available_compliance_checklists"
        )

    def test_staff_can_get_available_checklists(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.checklist.uuid.hex)

    def test_call_organizer_can_get_available_checklists(self):
        self.client.force_authenticate(self.fixture.call_organizer_user)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_user_without_permission_cannot_get_checklists(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_uuid_is_required(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("customer_uuid", response.data)

    def test_nonexistent_customer_returns_error(self):
        self.client.force_authenticate(self.fixture.staff)
        fake_uuid = uuid.uuid4().hex
        response = self.client.get(self.url, {"customer_uuid": fake_uuid})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Customer not found", str(response.data))

    def test_customer_without_call_managing_org_returns_error(self):
        customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"customer_uuid": customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Customer does not have a call managing organization", str(response.data)
        )

    def test_checklist_type_filtering(self):
        checklist_factories.ChecklistFactory(checklist_type="random")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url,
            {
                "customer_uuid": self.fixture.customer.uuid.hex,
                "checklist_type": ChecklistTypes.PROPOSAL_COMPLIANCE,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.checklist.uuid.hex)

    def test_response_includes_required_fields(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url, {"customer_uuid": self.fixture.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        checklist_data = response.data[0]
        self.assertIn("uuid", checklist_data)
        self.assertIn("name", checklist_data)
        self.assertIn("description", checklist_data)
        self.assertIn("checklist_type", checklist_data)
        self.assertIn("questions_count", checklist_data)
        self.assertIn("category_name", checklist_data)
        self.assertIn("category_uuid", checklist_data)


class CallProposalSlugTemplateSerializerTest(test.APITestCase):
    """Tests for proposal_slug_template field validation in Call serializer."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        # Create a fresh active call without proposals
        self.call = factories.CallFactory(
            manager=self.fixture.manager,
            state=CallStates.ACTIVE,
            created_by=self.fixture.owner,
        )
        # Add call_manager to the new call
        self.call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        self.url = factories.CallFactory.get_protected_url(self.call)

    def test_valid_template_accepted(self):
        """Valid template passes validation."""
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url, {"proposal_slug_template": "{call_slug}-{year}-{counter_padded}"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.call.refresh_from_db()
        self.assertEqual(
            self.call.proposal_slug_template,
            "{call_slug}-{year}-{counter_padded}",
        )

    def test_valid_template_with_all_variables(self):
        """Template with all allowed variables passes validation."""
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url,
            {
                "proposal_slug_template": "{org_slug}-{call_slug}-{round_slug}-{year}{month}-{counter}-{counter_padded}"
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_empty_template_accepted(self):
        """Empty template is accepted (uses default)."""
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(self.url, {"proposal_slug_template": ""})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_null_template_accepted(self):
        """Null template is accepted (uses default)."""
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(self.url, {"proposal_slug_template": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_placeholder_rejected(self):
        """Invalid placeholder raises validation error."""
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url, {"proposal_slug_template": "{invalid}-{counter_padded}"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid placeholders", str(response.data))
        self.assertIn("invalid", str(response.data))

    def test_multiple_invalid_placeholders_rejected(self):
        """Multiple invalid placeholders are listed in error."""
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url, {"proposal_slug_template": "{foo}-{bar}-{counter_padded}"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("bar", str(response.data))
        self.assertIn("foo", str(response.data))

    def test_malformed_template_rejected(self):
        """Malformed template raises validation error."""
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url,
            {"proposal_slug_template": "{call_slug"},  # Missing closing brace
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_change_template_with_proposals(self):
        """Cannot change template when proposals exist."""
        # Create a proposal to lock the template
        round_obj = factories.RoundFactory(call=self.call)
        factories.ProposalFactory(round=round_obj)

        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url, {"proposal_slug_template": "{call_slug}-{counter_padded}"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("proposals exist", str(response.data))

    def test_can_set_same_template_with_proposals(self):
        """Can set the same template value when proposals exist."""
        # Set initial template
        self.call.proposal_slug_template = "{call_slug}-{counter_padded}"
        self.call.save()

        # Create a proposal
        round_obj = factories.RoundFactory(call=self.call)
        factories.ProposalFactory(round=round_obj)

        # Setting the same value should be allowed
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.patch(
            self.url, {"proposal_slug_template": "{call_slug}-{counter_padded}"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_template_field_in_response(self):
        """Template field is included in API response."""
        self.call.proposal_slug_template = "{org_slug}-{counter_padded}"
        self.call.save()

        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("proposal_slug_template", response.data)
        self.assertEqual(
            response.data["proposal_slug_template"], "{org_slug}-{counter_padded}"
        )
