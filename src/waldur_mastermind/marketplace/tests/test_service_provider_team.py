from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions import enums
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    GlobalRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import (
    get_create_permission,
    get_delete_permission,
    get_update_permission,
    grant_global_role,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.utils import can_manage_invitation_with
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories

TEAM_PERMISSIONS = (
    PermissionEnum.CREATE_CUSTOMER_PERMISSION,
    PermissionEnum.UPDATE_CUSTOMER_PERMISSION,
    PermissionEnum.DELETE_CUSTOMER_PERMISSION,
)

TEAM_ACTIONS = ("add_user", "update_user", "delete_user")


def make_site_wide_holder():
    """A user whose site-wide role carries the team permissions.

    A plain user, so that no support bypass stands in for the grant.
    """
    holder = structure_factories.UserFactory()
    role = GlobalRole.get("GLOBAL.SUPPORT")
    for permission in TEAM_PERMISSIONS:
        role.add_permission(permission)
    grant_global_role(holder, role)
    return holder


class ServiceProviderTeamPermissionLookupTest(test.APITestCase):
    """The role permission tables are read by model name.

    A service provider's model name is ``serviceprovider``, so a table keyed
    only by the scope-type alias ``service_provider`` returned None and the
    team actions admitted staff alone (#986).
    """

    def test_team_permissions_resolve_for_service_provider(self):
        self.assertEqual(
            get_create_permission(models.ServiceProvider),
            PermissionEnum.CREATE_CUSTOMER_PERMISSION,
        )
        self.assertEqual(
            get_update_permission(models.ServiceProvider),
            PermissionEnum.UPDATE_CUSTOMER_PERMISSION,
        )
        self.assertEqual(
            get_delete_permission(models.ServiceProvider),
            PermissionEnum.DELETE_CUSTOMER_PERMISSION,
        )

    def test_every_bindable_scope_type_resolves(self):
        # The whole class of this bug: a scope whose model name differs from
        # its TYPE_MAP alias and has no model-name key.
        for scope_type, (app_label, model) in enums.BINDABLE_TYPE_MAP.items():
            model_class = ContentType.objects.get_by_natural_key(
                app_label, model
            ).model_class()
            for getter in (
                get_create_permission,
                get_update_permission,
                get_delete_permission,
            ):
                with self.subTest(scope_type=scope_type, getter=getter.__name__):
                    self.assertIsNotNone(getter(model_class))

    def test_both_keys_carry_the_same_permission(self):
        for table in (
            enums.CREATE_PERMISSIONS,
            enums.UPDATE_PERMISSIONS,
            enums.DELETE_PERMISSIONS,
        ):
            self.assertEqual(table["serviceprovider"], table["service_provider"])

    def test_site_wide_grant_stays_denied_for_service_provider_team(self):
        for table in (
            enums.CREATE_PERMISSIONS,
            enums.UPDATE_PERMISSIONS,
            enums.DELETE_PERMISSIONS,
        ):
            self.assertIn(table["serviceprovider"], enums.GLOBAL_DENIED_PERMISSIONS)


class ServiceProviderTeamApiTest(test.APITestCase):
    def setUp(self):
        self.service_provider = factories.ServiceProviderFactory()
        self.customer = self.service_provider.customer
        self.target = structure_factories.UserFactory()
        # permissions.yaml gives CUSTOMER.OWNER the team permissions; the test
        # database does not import permissions.yaml.
        for permission in TEAM_PERMISSIONS:
            CustomerRole.OWNER.add_permission(permission)
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)

    def _post(self, actor, action):
        self.client.force_authenticate(actor)
        return self.client.post(
            factories.ServiceProviderFactory.get_url(self.service_provider, action),
            {"user": self.target.uuid.hex, "role": ServiceProviderRole.MANAGER.name},
        )

    def _target_is_manager(self):
        return UserRole.objects.filter(
            user=self.target,
            role=ServiceProviderRole.MANAGER,
            object_id=self.service_provider.id,
            content_type__model="serviceprovider",
            is_active=True,
        ).exists()

    def _grant_target(self):
        self.service_provider.add_user(self.target, ServiceProviderRole.MANAGER)

    def test_owner_can_add_a_manager(self):
        response = self._post(self.owner, "add_user")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(self._target_is_manager())

    def test_owner_can_update_a_manager(self):
        self._grant_target()
        response = self._post(self.owner, "update_user")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_owner_can_remove_a_manager(self):
        self._grant_target()
        response = self._post(self.owner, "delete_user")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(self._target_is_manager())

    def test_staff_can_add_a_manager(self):
        response = self._post(
            structure_factories.UserFactory(is_staff=True), "add_user"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_site_wide_holder_cannot_manage_the_team(self):
        holder = make_site_wide_holder()
        self._grant_target()
        for action in TEAM_ACTIONS:
            with self.subTest(action=action):
                response = self._post(holder, action)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(self._target_is_manager())

    def test_owner_without_the_permissions_cannot_manage_the_team(self):
        for permission in TEAM_PERMISSIONS:
            CustomerRole.OWNER.permissions.filter(permission=permission.value).delete()
        self._grant_target()
        for action in TEAM_ACTIONS:
            with self.subTest(action=action):
                response = self._post(self.owner, action)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_cannot_manage_the_team(self):
        manager = structure_factories.UserFactory()
        self.service_provider.add_user(manager, ServiceProviderRole.MANAGER)
        self._grant_target()
        for action in TEAM_ACTIONS:
            with self.subTest(action=action):
                response = self._post(manager, action)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_of_another_organization_cannot_manage_the_team(self):
        other_owner = structure_factories.UserFactory()
        structure_factories.CustomerFactory().add_user(other_owner, CustomerRole.OWNER)
        self._grant_target()
        for action in TEAM_ACTIONS:
            with self.subTest(action=action):
                response = self._post(other_owner, action)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unrelated_user_cannot_manage_the_team(self):
        self._grant_target()
        for action in TEAM_ACTIONS:
            with self.subTest(action=action):
                response = self._post(structure_factories.UserFactory(), action)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_manage_invitations_to_the_team(self):
        # Invitations resolve the scope's permission through the same table.
        request = test.APIRequestFactory().get("/")
        request.user = self.owner
        self.assertTrue(can_manage_invitation_with(request, self.service_provider))
        holder = make_site_wide_holder()
        request.user = holder
        self.assertFalse(can_manage_invitation_with(request, self.service_provider))
