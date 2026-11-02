from unittest import mock

from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.structure.registry import SupportedServices
from waldur_core.structure.tests import factories


class ServiceSettingsListTest(test.APITestCase):
    def setUp(self):
        self.users = {
            "staff": factories.UserFactory(is_staff=True),
            "owner": factories.UserFactory(),
            "not_owner": factories.UserFactory(),
        }

        self.customers = {
            "owned": factories.CustomerFactory(),
            "inaccessible": factories.CustomerFactory(),
        }

        self.customers["owned"].add_user(self.users["owner"], CustomerRole.OWNER)

        self.settings = {
            "shared": factories.ServiceSettingsFactory(shared=True),
            "inaccessible": factories.ServiceSettingsFactory(
                customer=self.customers["inaccessible"]
            ),
            "owned": factories.ServiceSettingsFactory(
                customer=self.customers["owned"], backend_url="bk.url", password="123"
            ),
        }

        # Token is excluded, because it is not available for OpenStack
        self.credentials = ("backend_url", "username", "password")

    def test_user_can_see_shared_settings(self):
        self.client.force_authenticate(user=self.users["not_owner"])

        response = self.client.get(factories.ServiceSettingsFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)
        self.assert_credentials_hidden(response.data[0])
        self.assertEqual(
            response.data[0]["uuid"], self.settings["shared"].uuid.hex, response.data
        )

    def test_user_can_see_shared_and_own_settings(self):
        self.client.force_authenticate(user=self.users["owner"])

        response = self.client.get(factories.ServiceSettingsFactory.get_list_url())
        uuids_received = {d["uuid"] for d in response.data}
        uuids_expected = {self.settings[s].uuid.hex for s in ("shared", "owned")}
        self.assertEqual(uuids_received, uuids_expected, response.data)

    def test_admin_can_see_all_settings(self):
        self.client.force_authenticate(user=self.users["staff"])

        response = self.client.get(factories.ServiceSettingsFactory.get_list_url())
        uuids_received = {d["uuid"] for d in response.data}
        uuids_expected = {s.uuid.hex for s in self.settings.values()}
        self.assertEqual(uuids_received, uuids_expected, uuids_received)

    def test_user_can_see_credentials_of_own_settings(self):
        self.client.force_authenticate(user=self.users["owner"])

        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(self.settings["owned"])
        )
        self.assert_credentials_visible(response.data)

    def test_user_cant_see_others_settings(self):
        self.client.force_authenticate(user=self.users["not_owner"])

        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(self.settings["owned"])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_can_see_all_credentials(self):
        self.client.force_authenticate(user=self.users["staff"])

        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(self.settings["owned"])
        )
        self.assert_credentials_visible(response.data)

    def test_user_cant_see_shared_credentials(self):
        self.client.force_authenticate(user=self.users["owner"])

        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(self.settings["shared"])
        )
        self.assert_credentials_hidden(response.data)

    def assert_credentials_visible(self, data):
        for field in self.credentials:
            self.assertIn(field, data["options"])

    def assert_credentials_hidden(self, data):
        for field in self.credentials:
            self.assertNotIn(field, data["options"])


@override_config(PAT_ENABLED=True)
class ServiceSettingsTokenCredentialsTest(test.APITestCase):
    """A token sees credentials within its own limits, not its user's."""

    NARROW = [PermissionEnum.LIST_ORDERS.value]
    STAFF = [PermissionEnum.STAFF_ACCESS.value]
    INTEGRATION = [PermissionEnum.UPDATE_OFFERING_INTEGRATION.value]

    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.other_customer = factories.CustomerFactory()
        self.settings = factories.ServiceSettingsFactory(
            customer=self.customer, backend_url="bk.url", password="123"
        )
        self.staff = self.token_user(is_staff=True)
        self.owner = self.token_user()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.other_customer.add_user(self.owner, CustomerRole.OWNER)

    def token_user(self, **flags):
        user = factories.UserFactory(can_use_personal_access_tokens=True, **flags)
        Token.objects.get_or_create(user=user)
        return user

    def get_options(self, user, scopes, bindings=(), settings=None):
        pat = _create_pat(user, scopes=scopes, bindings=list(bindings))
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get(
            factories.ServiceSettingsFactory.get_url(settings or self.settings)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data["options"]

    def test_staff_token_needs_the_staff_scope(self):
        self.assertNotIn("password", self.get_options(self.staff, self.NARROW))

    def test_staff_token_with_the_staff_scope_sees_credentials(self):
        self.assertIn("password", self.get_options(self.staff, self.STAFF))

    def test_staff_token_bound_to_this_organization_sees_credentials(self):
        options = self.get_options(self.staff, self.STAFF, [self.customer])
        self.assertIn("password", options)

    def test_staff_token_bound_elsewhere_does_not_see_credentials(self):
        options = self.get_options(self.staff, self.STAFF, [self.other_customer])
        self.assertNotIn("password", options)

    def test_owner_token_needs_the_integration_permission(self):
        self.assertNotIn("password", self.get_options(self.owner, self.NARROW))

    def test_unbound_owner_token_with_the_permission_sees_credentials(self):
        self.assertIn("password", self.get_options(self.owner, self.INTEGRATION))

    def test_owner_token_bound_to_this_organization_sees_credentials(self):
        options = self.get_options(self.owner, self.INTEGRATION, [self.customer])
        self.assertIn("password", options)

    def test_owner_token_bound_elsewhere_does_not_see_credentials(self):
        options = self.get_options(self.owner, self.INTEGRATION, [self.other_customer])
        self.assertNotIn("password", options)

    def test_owner_token_bound_to_a_child_does_not_see_credentials(self):
        project = factories.ProjectFactory(customer=self.customer)

        options = self.get_options(self.owner, self.INTEGRATION, [project])

        self.assertNotIn("password", options)

    def test_non_owner_token_with_the_permission_does_not_see_credentials(self):
        stranger = self.token_user()
        self.settings.shared = True
        self.settings.save()

        self.assertNotIn("password", self.get_options(stranger, self.INTEGRATION))

    def test_staff_token_with_only_the_integration_permission_is_refused(self):
        # It reads an offering's service attributes as staff; these settings
        # need not belong to any offering, so they ask for the staff scope.
        self.assertNotIn("password", self.get_options(self.staff, self.INTEGRATION))

    def test_unbound_staff_token_reads_settings_of_no_organization(self):
        settings = factories.ServiceSettingsFactory(
            shared=True, backend_url="bk.url", password="456"
        )

        options = self.get_options(self.staff, self.STAFF, settings=settings)

        self.assertEqual(options["password"], "456")

    def test_bound_staff_token_does_not_see_settings_of_no_organization(self):
        settings = factories.ServiceSettingsFactory(
            shared=True, backend_url="bk.url", password="456"
        )

        options = self.get_options(
            self.staff, self.STAFF, [self.customer], settings=settings
        )

        self.assertNotIn("password", options)

    def test_list_decides_for_each_row(self):
        foreign = factories.ServiceSettingsFactory(
            customer=self.other_customer, backend_url="bk.url", password="789"
        )
        pat = _create_pat(self.staff, scopes=self.STAFF, bindings=[self.customer])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))

        response = self.client.get(factories.ServiceSettingsFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = {row["uuid"]: row["options"] for row in response.data}
        self.assertEqual(rows[self.settings.uuid.hex]["password"], "123")
        self.assertNotIn("password", rows[foreign.uuid.hex])


class ServiceBackendClassesTest(test.APITestCase):
    def setUp(self):
        self.service_settings = factories.ServiceSettingsFactory()

    def test_all_required_methods_are_implemented(self):
        for key in [s[0] for s in SupportedServices.get_choices()]:
            klass = SupportedServices.get_service_backend(key)
            try:
                klass(mock.MagicMock())
            except TypeError as e:
                self.fail(str(e))
