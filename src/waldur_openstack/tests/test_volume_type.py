from unittest import mock

from cinderclient.v3.volume_types import VolumeType as BackendVolumeType
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import models
from waldur_openstack.backend import OpenStackBackend
from waldur_openstack.tests import factories
from waldur_openstack.tests.fixtures import OpenStackFixture, mock_session


class VolumeTypeVisibilityTest(test.APITestCase):
    """A private Cinder volume type is shown only to those who may use it (#1072)."""

    def setUp(self):
        self.fixture = OpenStackFixture()
        self.provider = structure_factories.CustomerFactory()
        self.provider_owner = structure_factories.UserFactory()
        self.provider.add_user(self.provider_owner, CustomerRole.OWNER)
        self.cloud = factories.SettingsFactory(customer=self.provider, shared=True)
        self.public = factories.VolumeTypeFactory(
            settings=self.cloud, name="ssd", is_public=True
        )
        self.private = factories.VolumeTypeFactory(
            settings=self.cloud, name="acme-gold", is_public=False
        )
        private_cloud = factories.SettingsFactory(customer=self.provider, shared=False)
        self.private_cloud_type = factories.VolumeTypeFactory(
            settings=private_cloud, name="internal", is_public=True
        )

    def listed(self, user, **query):
        self.client.force_authenticate(user)
        response = self.client.get(factories.VolumeTypeFactory.get_list_url(), query)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    def names(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.VolumeTypeFactory.get_list_url() + "names/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return set(response.data)

    def test_user_without_role_sees_only_public_types(self):
        user = structure_factories.UserFactory()
        self.assertEqual(
            self.listed(user),
            {self.public.uuid.hex, self.private_cloud_type.uuid.hex},
        )

    def test_order_form_lists_public_types_of_the_offering_cloud(self):
        user = structure_factories.UserFactory()
        self.assertEqual(
            self.listed(user, settings_uuid=self.cloud.uuid.hex),
            {self.public.uuid.hex},
        )

    def test_user_without_role_cannot_retrieve_private_type(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(factories.VolumeTypeFactory.get_url(self.private))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_names_leave_out_private_types(self):
        user = structure_factories.UserFactory()
        self.assertEqual(self.names(user), {"ssd", "internal"})

    def test_users_of_a_tenant_see_the_private_types_assigned_to_it(self):
        tenant = factories.TenantFactory(
            service_settings=self.cloud, project=self.fixture.project
        )
        self.private.tenants.add(tenant)
        for user in (self.fixture.owner, self.fixture.admin, self.fixture.manager):
            with self.subTest(user=user):
                self.assertEqual(
                    self.listed(user, tenant_uuid=tenant.uuid.hex),
                    {self.private.uuid.hex},
                )
                self.assertIn("acme-gold", self.names(user))

    def test_users_of_another_tenant_do_not_see_the_private_type(self):
        tenant = factories.TenantFactory(
            service_settings=self.cloud, project=self.fixture.project
        )
        other_tenant = factories.TenantFactory(service_settings=self.cloud)
        self.private.tenants.add(other_tenant)
        self.public.tenants.add(tenant)
        listed = self.listed(self.fixture.owner)
        self.assertIn(self.public.uuid.hex, listed)
        self.assertNotIn(self.private.uuid.hex, listed)
        self.assertEqual(
            self.listed(self.fixture.owner, tenant_uuid=other_tenant.uuid.hex), set()
        )

    def test_provider_owner_sees_all_types_of_own_clouds(self):
        self.assertEqual(
            self.listed(self.provider_owner),
            {
                self.public.uuid.hex,
                self.private.uuid.hex,
                self.private_cloud_type.uuid.hex,
            },
        )

    def test_staff_and_support_see_every_type(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            with self.subTest(user=user):
                self.assertTrue(
                    {
                        self.public.uuid.hex,
                        self.private.uuid.hex,
                        self.private_cloud_type.uuid.hex,
                    }
                    <= self.listed(user)
                )
                self.assertTrue({"ssd", "acme-gold", "internal"} <= self.names(user))

    def test_type_shared_by_several_tenants_is_listed_once(self):
        project = self.fixture.project
        for _ in range(2):
            self.private.tenants.add(
                factories.TenantFactory(service_settings=self.cloud, project=project)
            )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.VolumeTypeFactory.get_list_url())
        uuids = [row["uuid"] for row in response.data]
        self.assertEqual(uuids.count(self.private.uuid.hex), 1)

    @override_config(PAT_ENABLED=True)
    def test_token_without_staff_or_support_scope_sees_what_roles_reach(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            with self.subTest(user=user):
                use_pat(self.client, user, PermissionEnum.LIST_RESOURCES.value)
                response = self.client.get(factories.VolumeTypeFactory.get_list_url())
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    {row["uuid"] for row in response.data},
                    {self.public.uuid.hex, self.private_cloud_type.uuid.hex},
                )

    def test_unauthenticated_user_cannot_list(self):
        response = self.client.get(factories.VolumeTypeFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


def backend_volume_type(backend_id, name, **info):
    return BackendVolumeType(
        None,
        {"id": backend_id, "name": name, "description": "", **info},
        loaded=True,
    )


class PullVolumeTypesTest(test.APITestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()
        self.cinder = mock.patch("waldur_openstack.backend.get_cinder_client").start()()
        mock_session()
        self.backend = OpenStackBackend(self.fixture.settings)

    def tearDown(self):
        mock.patch.stopall()

    def remote_types(self):
        return [
            backend_volume_type(
                "vt-public", "ssd", **{"os-volume-type-access:is_public": True}
            ),
            backend_volume_type(
                "vt-private", "acme-gold", **{"os-volume-type-access:is_public": False}
            ),
            # Cinder without the type access extension reports no flag.
            backend_volume_type("vt-legacy", "hdd"),
        ]

    def assert_flags_recorded(self):
        flags = dict(
            models.VolumeType.objects.filter(
                settings=self.fixture.settings
            ).values_list("backend_id", "is_public")
        )
        self.assertEqual(
            flags, {"vt-public": True, "vt-private": False, "vt-legacy": True}
        )

    def test_global_pull_records_whether_type_is_public(self):
        self.cinder.volume_types.list.return_value = self.remote_types()
        self.backend.pull_global_volume_types()
        self.assert_flags_recorded()

    def test_tenant_pull_records_whether_type_is_public(self):
        self.cinder.volume_types.list.return_value = self.remote_types()
        self.backend.pull_tenant_volume_types(self.fixture.tenant)
        self.assert_flags_recorded()
        self.assertEqual(self.fixture.tenant.volume_types.count(), 3)

    def test_type_made_private_is_updated(self):
        factories.VolumeTypeFactory(
            settings=self.fixture.settings, backend_id="vt-private", is_public=True
        )
        self.cinder.volume_types.list.return_value = self.remote_types()
        self.backend.pull_global_volume_types()
        self.assert_flags_recorded()
