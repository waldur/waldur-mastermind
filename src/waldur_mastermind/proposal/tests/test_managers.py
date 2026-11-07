from ddt import data, ddt
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import CallStates, ProposalStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class ManagerGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call_manager = self.fixture.manager

    @data(
        "staff",
        "owner",
        "user",
        "customer_support",
    )
    def test_manager_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallManagingOrganisationFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_manager_should_be_visible_to_unauthenticated_users_by_default(
        self,
    ):
        url = factories.CallManagingOrganisationFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_manager_should_be_invisible_to_unauthenticated_users_when_offerings_are_public(
        self,
    ):
        url = factories.CallManagingOrganisationFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class ManagerCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data("staff")
    def test_staff_can_create_manager(self, user):
        response = self.create_manager(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.CallManagingOrganisation.objects.filter(
                customer=self.customer
            ).exists()
        )

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_create_manager(self, user):
        response = self.create_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_create_manager_with_settings_enabled(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.REGISTER_SERVICE_PROVIDER)
        response = self.create_manager("owner")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("owner")
    def test_owner_can_not_create_manager_with_settings_disabled(self, user):
        response = self.create_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_create_manager_with_settings_disabled(
        self, user
    ):
        response = self.create_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_manager(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallManagingOrganisationFactory.get_list_url()

        payload = {
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
        }

        return self.client.post(url, payload)


@ddt
class ManagerUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer

    @data("staff", "owner")
    def test_authorized_user_can_update_manager(self, user):
        response, call_manager = self.update_manager(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        call_manager.refresh_from_db()
        self.assertEqual(call_manager.description, "new description")
        self.assertTrue(
            models.CallManagingOrganisation.objects.filter(
                customer=self.customer
            ).exists()
        )

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_update_manager(self, user):
        response, call_manager = self.update_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_manager(self, user, payload=None, **kwargs):
        if not payload:
            payload = {"description": "new description"}

        call_manager = factories.CallManagingOrganisationFactory(customer=self.customer)
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallManagingOrganisationFactory.get_url(call_manager)

        response = self.client.patch(url, payload, **kwargs)
        call_manager.refresh_from_db()

        return response, call_manager

    def test_upload_image(self):
        payload = {"image": dummy_image()}
        response, call_manager = self.update_manager(
            "staff", payload=payload, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(call_manager.image)

        url = factories.CallManagingOrganisationFactory.get_url(call_manager)
        response = self.client.patch(url, {"image": None})
        call_manager.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(call_manager.image)


@ddt
class ManagerDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.call_manager = factories.CallManagingOrganisationFactory(
            customer=self.customer
        )

    @data("staff", "owner")
    def test_authorized_user_can_delete_manager(self, user):
        response = self.delete_manager(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.CallManagingOrganisation.objects.filter(
                customer=self.customer
            ).exists()
        )

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_delete_manager(self, user):
        response = self.delete_manager(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.CallManagingOrganisation.objects.filter(
                customer=self.customer
            ).exists()
        )

    def delete_manager(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallManagingOrganisationFactory.get_url(self.call_manager)
        response = self.client.delete(url)
        return response


class OrganizationVisibilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    def test_call_organizer_sees_organization(self):
        customer = self.fixture.customer
        url = structure_factories.CustomerFactory.get_url(customer)

        self.client.force_authenticate(self.fixture.call_organizer_user)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class ManagerStatsAccessTest(test.APITestCase):
    """Who may read an organisation's call statistics (#1108): those who may
    read its reports, over the calls the reports cover."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        # Two active calls; the fixture's call manager manages the first.
        self.fixture.call
        self.other_call = factories.CallFactory(
            manager=self.fixture.manager, state=CallStates.ACTIVE
        )
        self.url = factories.CallManagingOrganisationFactory.get_url(
            self.fixture.manager, "stats"
        )

    @data("staff", "global_support", "call_organizer_user")
    def test_organisation_wide_readers_count_every_call(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["open_calls"], 2)

    def test_call_manager_counts_only_their_own_calls(self):
        # A pending proposal in the call they do not manage, which an
        # organisation-wide reader counts.
        factories.ProposalFactory(
            round=factories.RoundFactory(call=self.other_call),
            state=ProposalStates.SUBMITTED,
        )
        self.client.force_authenticate(self.fixture.staff)
        everyone = self.client.get(self.url).data
        self.client.force_authenticate(self.fixture.call_manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["open_calls"], 1)
        self.assertEqual(
            response.data["pending_proposals"], everyone["pending_proposals"] - 1
        )

    # ``user`` holds no role at all.
    @data("owner", "customer_support", "user")
    def test_users_without_call_update_cannot_read_stats(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # Refused already: PublicViewsetMixin opens only list and retrieve.
    @data(True, False)
    def test_anonymous_user_cannot_read_stats(self, public_catalog):
        with override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=public_catalog):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
@override_config(PAT_ENABLED=True)
class ManagerStatsTokenTest(test.APITestCase):
    """A personal access token reads the statistics only within its scopes and
    bindings, as the CALL.UPDATE check it stands in for would allow."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.fixture.call
        self.url = factories.CallManagingOrganisationFactory.get_url(
            self.fixture.manager, "stats"
        )

    def get_with_token(self, user, scopes, bindings=()):
        user.can_use_personal_access_tokens = True
        user.save()
        pat = _create_pat(user, scopes=[s.value for s in scopes], bindings=bindings)
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        return self.client.get(self.url)

    @data("call_organizer_user", "call_manager")
    def test_token_with_call_update_reads_stats(self, user):
        response = self.get_with_token(
            getattr(self.fixture, user), [PermissionEnum.UPDATE_CALL]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["open_calls"], 1)

    @data("call_organizer_user", "call_manager")
    def test_token_without_call_update_cannot_read_stats(self, user):
        response = self.get_with_token(
            getattr(self.fixture, user), [PermissionEnum.LIST_PROJECTS]
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("call_organizer_user", "call_manager")
    def test_token_bound_elsewhere_cannot_read_stats(self, user):
        response = self.get_with_token(
            getattr(self.fixture, user),
            [PermissionEnum.UPDATE_CALL],
            [structure_factories.CustomerFactory()],
        )
        # The binding hides the organisation itself.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
