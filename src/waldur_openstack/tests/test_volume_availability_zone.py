from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories

from . import factories, fixtures


@ddt
class VolumeAvailabilityZoneTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.url = factories.VolumeAvailabilityZoneFactory.get_list_url()
        self.fixture.volume

    @data("admin", "manager", "staff")
    def test_authorized_users_can_get_volume_availability_zone_list(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_unauthorized_users_cannot_get_volume_availability_zone_list(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class AvailabilityZoneVisibilityTest(test.APITestCase):
    """Zones are shown with the cloud they belong to (#1072)."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.provider = structure_factories.CustomerFactory()
        self.shared_cloud = factories.SettingsFactory(
            customer=self.provider, shared=True
        )
        self.private_cloud = factories.SettingsFactory(
            customer=self.provider, shared=False
        )

    def zones(self, factory):
        # Zones are pulled per service settings and carry no tenant.
        return (
            factory(settings=self.shared_cloud, tenant=None),
            factory(settings=self.private_cloud, tenant=None),
        )

    def listed(self, factory, user):
        self.client.force_authenticate(user)
        response = self.client.get(factory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    @data(
        factories.VolumeAvailabilityZoneFactory,
        factories.InstanceAvailabilityZoneFactory,
    )
    def test_user_without_role_sees_zones_of_shared_clouds_only(self, factory):
        shared_zone, private_zone = self.zones(factory)
        user = structure_factories.UserFactory()
        self.assertEqual(self.listed(factory, user), {shared_zone.uuid.hex})
        response = self.client.get(factory.get_url(private_zone))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        factories.VolumeAvailabilityZoneFactory,
        factories.InstanceAvailabilityZoneFactory,
    )
    def test_users_of_a_tenant_see_zones_of_its_private_cloud(self, factory):
        _, private_zone = self.zones(factory)
        factories.TenantFactory(
            service_settings=self.private_cloud, project=self.fixture.project
        )
        self.assertIn(private_zone.uuid.hex, self.listed(factory, self.fixture.admin))

    @data(
        factories.VolumeAvailabilityZoneFactory,
        factories.InstanceAvailabilityZoneFactory,
    )
    def test_provider_users_see_zones_of_own_private_cloud(self, factory):
        _, private_zone = self.zones(factory)
        owner = structure_factories.UserFactory()
        self.provider.add_user(owner, CustomerRole.OWNER)
        self.assertIn(private_zone.uuid.hex, self.listed(factory, owner))

    @data(
        factories.VolumeAvailabilityZoneFactory,
        factories.InstanceAvailabilityZoneFactory,
    )
    def test_staff_and_support_see_every_zone(self, factory):
        zones = {zone.uuid.hex for zone in self.zones(factory)}
        for user in (self.fixture.staff, self.fixture.global_support):
            with self.subTest(user=user):
                self.assertTrue(zones <= self.listed(factory, user))
