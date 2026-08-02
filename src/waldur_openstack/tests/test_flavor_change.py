from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_core.permissions.fixtures import ProjectRole
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.models import Instance, Tenant

from . import factories, fixtures


@ddt
class FlavorListRetrieveTestCase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.flavor = self.fixture.flavor
        self.url = factories.FlavorFactory.get_list_url()

    @data("staff", "owner", "service_manager", "admin", "manager")
    def test_user_can_get_flavors_list(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@ddt
class FlavorChangeInstanceTestCase(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.instance.runtime_state = "SHUTOFF"
        self.instance.state = CoreStates.OK
        self.instance.save(update_fields=["runtime_state", "state"])
        self.settings = self.fixture.tenant.service_settings

        self.url = factories.InstanceFactory.get_url(
            self.instance, action="change_flavor"
        )

    @data("admin", "manager")
    def test_authorized_user_can_change_flavor_of_stopped_instance(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        new_flavor = factories.FlavorFactory(
            settings=self.settings, disk=self.instance.disk + 1
        )
        new_flavor.tenants.add(self.fixture.tenant)

        data = {"flavor": factories.FlavorFactory.get_url(new_flavor)}
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(
            reread_instance.disk,
            self.instance.disk,
            "Instance disk size should not have changed",
        )
        self.assertEqual(
            reread_instance.state,
            CoreStates.UPDATE_SCHEDULED,
            "Instance should have been scheduled to flavor change",
        )

    def test_when_flavor_is_changed_related_quotas_are_updated(self):
        Quotas = Tenant.Quotas

        new_flavor = factories.FlavorFactory(
            settings=self.settings,
            cores=self.instance.cores + 1,
            ram=self.instance.ram + 1024,
        )
        new_flavor.tenants.add(self.fixture.tenant)

        self.instance.service_settings.add_quota_usage(Quotas.vcpu, self.instance.cores)
        self.instance.service_settings.add_quota_usage(Quotas.ram, self.instance.ram)

        self.fixture.tenant.add_quota_usage(Quotas.vcpu, self.instance.cores)
        self.fixture.tenant.add_quota_usage(Quotas.ram, self.instance.ram)

        data = {"flavor": factories.FlavorFactory.get_url(new_flavor)}

        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        quota_holders = [self.instance.service_settings, self.fixture.tenant]

        for holder in quota_holders:
            vcpu_usage = holder.get_quota_usage(Quotas.vcpu)
            ram_usage = holder.get_quota_usage(Quotas.ram)

            self.assertEqual(vcpu_usage, self.instance.cores + 1)
            self.assertEqual(ram_usage, self.instance.ram + 1024)

    def test_user_can_change_flavor_to_flavor_with_less_cpu_if_result_cpu_quota_usage_is_less_then_cpu_limit(
        self,
    ):
        self.client.force_authenticate(user=self.fixture.admin)
        instance = self.instance
        instance.cores = 5
        instance.save()

        new_flavor = factories.FlavorFactory(
            settings=self.settings,
            disk=self.instance.disk + 1,
            cores=instance.cores - 1,
        )
        new_flavor.tenants.add(self.fixture.tenant)

        data = {"flavor": factories.FlavorFactory.get_url(new_flavor)}

        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(
            reread_instance.state,
            CoreStates.UPDATE_SCHEDULED,
            "Instance should have been scheduled for flavor change",
        )

    def test_user_cannot_change_instance_flavor_without_flavor_in_request(self):
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_change_flavor_to_flavor_with_less_ram_if_result_ram_quota_usage_is_less_then_ram_limit(
        self,
    ):
        self.client.force_authenticate(user=self.fixture.admin)
        instance = self.instance
        instance.cores = 5
        instance.save()

        new_flavor = factories.FlavorFactory(
            settings=self.settings,
            disk=self.instance.disk + 1,
            ram=instance.ram - 1,
        )
        new_flavor.tenants.add(self.fixture.tenant)
        data = {"flavor": factories.FlavorFactory.get_url(new_flavor)}

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(
            reread_instance.state,
            CoreStates.UPDATE_SCHEDULED,
            "Instance should have been scheduled for flavor change",
        )

    @data("admin", "manager")
    def test_authorized_user_cannot_change_flavor_of_stopped_instance_if_tenant_quota_would_be_exceeded(
        self, user
    ):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        self.instance.tenant.set_quota_limit("ram", 1024)
        self.instance.tenant.set_quota_limit("vcpu", 10)

        # check for ram
        big_ram_flavor = factories.FlavorFactory(
            settings=self.settings,
            ram=self.instance.tenant.get_quota_limit("ram") * 10,
        )
        big_ram_flavor.tenants.add(self.fixture.tenant)
        data = {"flavor": factories.FlavorFactory.get_url(big_ram_flavor)}
        response = self.client.post(self.url, data)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

        # check for vcpu
        many_core_flavor = factories.FlavorFactory(
            settings=self.settings,
            cores=self.instance.tenant.get_quota_limit("vcpu") * 10,
        )
        many_core_flavor.tenants.add(self.fixture.tenant)
        data = {"flavor": factories.FlavorFactory.get_url(many_core_flavor)}
        response = self.client.post(self.url, data)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_user_cannot_use_flavor_not_connected_to_the_tenant(self):
        self.client.force_authenticate(user=self.fixture.admin)

        new_flavor = factories.FlavorFactory(disk=self.instance.disk + 1)

        response = self.client.post(
            self.url, {"flavor": factories.FlavorFactory.get_url(new_flavor)}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("flavor", response.data)
        self.assertEqual(
            response.data["flavor"], ["New flavor is not visible in tenant."]
        )

        reread_instance = Instance.objects.get(pk=self.instance.pk)

        self.assertEqual(
            reread_instance.disk, self.instance.disk, "Instance disk not have changed"
        )

    def test_user_cannot_change_flavor_of_instance_he_has_no_role_in(self):
        self.client.force_authenticate(user=self.fixture.admin)

        inaccessible_instance = factories.InstanceFactory()

        new_flavor = factories.FlavorFactory(
            settings=inaccessible_instance.service_settings,
            disk=self.instance.disk + 1,
        )
        new_flavor.tenants.add(self.fixture.tenant)

        response = self.client.post(
            factories.InstanceFactory.get_url(
                inaccessible_instance, action="change_flavor"
            ),
            {"flavor": factories.FlavorFactory.get_url(new_flavor)},
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        reread_instance = Instance.objects.get(pk=inaccessible_instance.pk)
        self.assertEqual(
            reread_instance.disk,
            inaccessible_instance.disk,
            "Instance disk not have changed",
        )

    def test_user_cannot_flavor_change_instance_in_creation_scheduled_state(self):
        self.client.force_authenticate(user=self.fixture.user)

        instance = factories.InstanceFactory(state=CoreStates.CREATION_SCHEDULED)
        project = instance.project
        project.add_user(self.fixture.user, ProjectRole.ADMIN)

        response = self.client.post(
            factories.InstanceFactory.get_url(instance, action="change_flavor"), {}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_cannot_change_flavor_of_non_offline_instance(self):
        self.client.force_authenticate(user=self.fixture.user)

        # Check all states but deleted and offline
        forbidden_states = [
            state
            for (state, _) in CoreStates.choices
            if state not in (CoreStates.DELETING, CoreStates.OK)
        ]

        for state in forbidden_states:
            instance = factories.InstanceFactory(state=state)
            instance.project.add_user(self.fixture.user, ProjectRole.ADMIN)

            changed_flavor = factories.FlavorFactory(settings=instance.service_settings)
            changed_flavor.tenants.add(self.fixture.tenant)

            data = {"flavor": factories.FlavorFactory.get_url(changed_flavor)}

            response = self.client.post(
                factories.InstanceFactory.get_url(instance, action="change_flavor"),
                data,
            )

            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

            reread_instance = Instance.objects.get(pk=instance.pk)
            self.assertEqual(
                reread_instance.disk, instance.disk, "Instance disk not have changed"
            )

    def test_user_cannot_flavor_change_with_empty_parameters(self):
        self.client.force_authenticate(user=self.fixture.user)

        instance = factories.InstanceFactory(
            state=CoreStates.OK,
            runtime_state=Instance.RuntimeStates.SHUTOFF,
        )
        project = instance.project

        project.add_user(self.fixture.user, ProjectRole.ADMIN)

        response = self.client.post(
            factories.InstanceFactory.get_url(instance, action="change_flavor"), data={}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class FlavorOfferingFilterTestCase(test.APITestCase):
    """Filtering service properties by offering must handle both scope kinds.

    An offering's scope is a generic relation. The tenant-provisioning offering
    points at the service settings, while the per-tenant instance and volume
    offerings Waldur creates alongside a tenant point at the tenant. Assuming
    settings meant a tenant-scoped offering hit a Tenant queryset with a Tenant
    instance, which Django rejects — the endpoint answered 500.
    """

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.flavor = self.fixture.flavor
        self.tenant = self.fixture.tenant
        self.flavor.tenants.add(self.tenant)
        self.url = factories.FlavorFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def _offering(self, scope):
        return marketplace_factories.OfferingFactory(
            customer=self.fixture.customer, scope=scope
        )

    def test_offering_scoped_to_a_tenant_is_filtered_not_an_error(self):
        offering = self._offering(self.tenant)

        response = self.client.get(self.url, {"offering_uuid": offering.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [flavor["uuid"] for flavor in response.data], [self.flavor.uuid.hex]
        )

    def test_offering_scoped_to_service_settings_still_works(self):
        offering = self._offering(self.fixture.settings)

        response = self.client.get(self.url, {"offering_uuid": offering.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [flavor["uuid"] for flavor in response.data], [self.flavor.uuid.hex]
        )

    def test_offering_with_an_unrelated_scope_returns_nothing(self):
        offering = self._offering(self.fixture.project)

        response = self.client.get(self.url, {"offering_uuid": offering.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_offering_without_a_scope_returns_nothing(self):
        offering = self._offering(None)

        response = self.client.get(self.url, {"offering_uuid": offering.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
