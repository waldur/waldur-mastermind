from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_openstack.openstack.models import Tenant
from waldur_openstack.openstack_tenant.models import Instance

from . import factories, fixtures


@ddt
class FlavorChangeInstanceTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance
        self.instance.runtime_state = 'SHUTOFF'
        self.instance.state = Instance.States.OK
        self.instance.save(update_fields=['runtime_state', 'state'])
        self.settings = self.fixture.openstack_tenant_service_settings

        self.url = factories.InstanceFactory.get_url(
            self.instance, action='change_flavor'
        )

    @data('admin', 'manager')
    def test_authorized_user_can_change_flavor_of_stopped_instance(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        new_flavor = factories.FlavorFactory(
            settings=self.settings, disk=self.instance.disk + 1
        )

        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(
            reread_instance.disk,
            self.instance.disk,
            'Instance disk size should not have changed',
        )
        self.assertEqual(
            reread_instance.state,
            Instance.States.UPDATE_SCHEDULED,
            'Instance should have been scheduled to flavor change',
        )

    def test_when_flavor_is_changed_related_quotas_are_updated(self):
        Quotas = Tenant.Quotas

        new_flavor = factories.FlavorFactory(
            settings=self.settings,
            cores=self.instance.cores + 1,
            ram=self.instance.ram + 1024,
        )

        self.settings.add_quota_usage(Quotas.vcpu, self.instance.cores)
        self.settings.add_quota_usage(Quotas.ram, self.instance.ram)

        self.fixture.tenant.add_quota_usage(Quotas.vcpu, self.instance.cores)
        self.fixture.tenant.add_quota_usage(Quotas.ram, self.instance.ram)

        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}

        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        quota_holders = [self.settings, self.fixture.tenant]

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

        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}

        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(
            reread_instance.state,
            Instance.States.UPDATE_SCHEDULED,
            'Instance should have been scheduled for flavor change',
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
        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(
            reread_instance.state,
            Instance.States.UPDATE_SCHEDULED,
            'Instance should have been scheduled for flavor change',
        )

    @data('admin', 'manager')
    def test_authorized_user_cannot_change_flavor_of_stopped_instance_if_settings_quota_would_be_exceeded(
        self, user
    ):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        self.settings.set_quota_limit('ram', 1024)
        self.settings.set_quota_limit('vcpu', 10)

        # check for ram
        big_ram_flavor = factories.FlavorFactory(
            settings=self.settings, ram=self.settings.get_quota_limit('ram') * 10
        )
        data = {'flavor': factories.FlavorFactory.get_url(big_ram_flavor)}
        response = self.client.post(self.url, data)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

        # check for vcpu
        many_core_flavor = factories.FlavorFactory(
            settings=self.settings, cores=self.settings.get_quota_limit('vcpu') * 10
        )
        data = {'flavor': factories.FlavorFactory.get_url(many_core_flavor)}
        response = self.client.post(self.url, data)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_user_cannot_change_flavor_to_flavor_from_different_service(self):
        self.client.force_authenticate(user=self.fixture.admin)

        new_flavor = factories.FlavorFactory(disk=self.instance.disk + 1)

        response = self.client.post(
            self.url, {'flavor': factories.FlavorFactory.get_url(new_flavor)}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertDictContainsSubset(
            {'flavor': ['New flavor is not within the same service settings']},
            response.data,
        )

        reread_instance = Instance.objects.get(pk=self.instance.pk)

        self.assertEqual(
            reread_instance.disk, self.instance.disk, 'Instance disk not have changed'
        )

    def test_user_cannot_change_flavor_of_instance_he_has_no_role_in(self):
        self.client.force_authenticate(user=self.fixture.admin)

        inaccessible_instance = factories.InstanceFactory()

        new_flavor = factories.FlavorFactory(
            settings=inaccessible_instance.service_settings,
            disk=self.instance.disk + 1,
        )

        response = self.client.post(
            factories.InstanceFactory.get_url(
                inaccessible_instance, action='change_flavor'
            ),
            {'flavor': factories.FlavorFactory.get_url(new_flavor)},
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        reread_instance = Instance.objects.get(pk=inaccessible_instance.pk)
        self.assertEqual(
            reread_instance.disk,
            inaccessible_instance.disk,
            'Instance disk not have changed',
        )

    def test_user_cannot_flavor_change_instance_in_creation_scheduled_state(self):
        self.client.force_authenticate(user=self.fixture.user)

        instance = factories.InstanceFactory(state=Instance.States.CREATION_SCHEDULED)
        project = instance.project
        project.add_user(self.fixture.user, ProjectRole.ADMIN)

        response = self.client.post(
            factories.InstanceFactory.get_url(instance, action='change_flavor'), {}
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_cannot_change_flavor_of_non_offline_instance(self):
        self.client.force_authenticate(user=self.fixture.user)

        # Check all states but deleted and offline
        forbidden_states = [
            state
            for (state, _) in Instance.States.CHOICES
            if state not in (Instance.States.DELETING, Instance.States.OK)
        ]

        for state in forbidden_states:
            instance = factories.InstanceFactory(state=state)
            instance.project.add_user(self.fixture.user, ProjectRole.ADMIN)

            changed_flavor = factories.FlavorFactory(settings=instance.service_settings)

            data = {'flavor': factories.FlavorFactory.get_url(changed_flavor)}

            response = self.client.post(
                factories.InstanceFactory.get_url(instance, action='change_flavor'),
                data,
            )

            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

            reread_instance = Instance.objects.get(pk=instance.pk)
            self.assertEqual(
                reread_instance.disk, instance.disk, 'Instance disk not have changed'
            )

    def test_user_cannot_flavor_change_with_empty_parameters(self):
        self.client.force_authenticate(user=self.fixture.user)

        instance = factories.InstanceFactory(
            state=Instance.States.OK,
            runtime_state=Instance.RuntimeStates.SHUTOFF,
        )
        project = instance.project

        project.add_user(self.fixture.user, ProjectRole.ADMIN)

        response = self.client.post(
            factories.InstanceFactory.get_url(instance, action='change_flavor'), data={}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
