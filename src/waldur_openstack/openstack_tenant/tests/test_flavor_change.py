from ddt import ddt, data
from rest_framework import test, status

from waldur_core.structure.models import ProjectRole

from ..models import Instance, OpenStackTenantServiceProjectLink
from . import factories, fixtures


@ddt
class FlavorChangeInstanceTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance
        self.instance.runtime_state = 'SHUTOFF'
        self.instance.state = Instance.States.OK
        self.instance.save(update_fields=['runtime_state', 'state'])

        # User manages managed_instance through its project group
        self.managed_instance = factories.InstanceFactory(
            state=Instance.States.OK,
            runtime_state=Instance.RuntimeStates.SHUTOFF,
        )

    @data('admin', 'manager')
    def test_user_with_access_can_change_flavor_of_stopped_instance(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        new_flavor = factories.FlavorFactory(
            settings=self.fixture.openstack_tenant_service_settings,
            disk=self.instance.disk + 1
        )

        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}
        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='change_flavor'), data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(reread_instance.disk, self.instance.disk,
                         'Instance disk size should not have changed')
        self.assertEqual(reread_instance.state, Instance.States.UPDATE_SCHEDULED,
                         'Instance should have been scheduled to flavor change')

    def test_when_flavor_is_changed_spl_quota_is_updated(self):
        self.client.force_authenticate(user=self.fixture.admin)

        new_flavor = factories.FlavorFactory(
            settings=self.fixture.openstack_tenant_service_settings,
            cores=self.instance.cores + 1,
            ram=self.instance.ram + 1024,
        )

        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}
        url = factories.InstanceFactory.get_url(self.instance, action='change_flavor')

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        Quotas = OpenStackTenantServiceProjectLink.Quotas
        quotas = self.fixture.spl.quotas

        self.assertEqual(quotas.get(name=Quotas.vcpu).usage, self.instance.cores + 1)
        self.assertEqual(quotas.get(name=Quotas.ram).usage, self.instance.ram + 1024)

    def test_user_can_change_flavor_to_flavor_with_less_cpu_if_result_cpu_quota_usage_is_less_then_cpu_limit(self):
        self.client.force_authenticate(user=self.fixture.admin)
        instance = self.instance
        instance.cores = 5
        instance.save()

        new_flavor = factories.FlavorFactory(
            settings=self.fixture.openstack_tenant_service_settings,
            disk=self.instance.disk + 1,
            cores=instance.cores - 1,
        )

        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}

        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='change_flavor'), data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(reread_instance.state, Instance.States.UPDATE_SCHEDULED,
                         'Instance should have been scheduled for flavor change')

    def test_user_cannot_change_instance_flavor_without_flavor_in_request(self):
        self.client.force_authenticate(user=self.fixture.admin)
        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='change_flavor'), {})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_change_flavor_to_flavor_with_less_ram_if_result_ram_quota_usage_is_less_then_ram_limit(self):
        self.client.force_authenticate(user=self.fixture.admin)
        instance = self.instance
        instance.cores = 5
        instance.save()

        new_flavor = factories.FlavorFactory(
            settings=self.fixture.openstack_tenant_service_settings,
            disk=self.instance.disk + 1,
            ram=instance.ram - 1,
        )
        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}

        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='change_flavor'), data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        reread_instance = Instance.objects.get(pk=self.instance.pk)
        self.assertEqual(reread_instance.state, Instance.States.UPDATE_SCHEDULED,
                         'Instance should have been scheduled for flavor change')

    @data('admin', 'manager')
    def test_user_with_access_cannot_change_flavor_of_stopped_instance_if_spl_quota_would_be_exceeded(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        settings = self.fixture.openstack_tenant_service_settings
        spl = self.fixture.spl

        self._assert_bad_request_is_raised_if_vcpu_or_ram_quotas_exceed_quotas_holder_limit(settings, spl)

    @data('admin', 'manager')
    def test_user_with_access_cannot_change_flavor_of_stopped_instance_if_settings_quota_would_be_exceeded(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        settings = self.fixture.openstack_tenant_service_settings

        self._assert_bad_request_is_raised_if_vcpu_or_ram_quotas_exceed_quotas_holder_limit(settings, settings)

    def _assert_bad_request_is_raised_if_vcpu_or_ram_quotas_exceed_quotas_holder_limit(self, settings, quotas_holder):
        quotas_holder.set_quota_limit('ram', 1024)

        # check for ram
        big_ram_flavor = factories.FlavorFactory(
            settings=settings,
            ram=quotas_holder.quotas.get(name='ram').limit + self.instance.ram + 1,
        )
        data = {'flavor': factories.FlavorFactory.get_url(big_ram_flavor)}
        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='change_flavor'), data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

        # check for vcpu
        many_core_flavor = factories.FlavorFactory(
            settings=settings,
            cores=quotas_holder.quotas.get(name='vcpu').limit + self.instance.cores + 1,
        )
        data = {'flavor': factories.FlavorFactory.get_url(many_core_flavor)}
        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='change_flavor'), data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_user_cannot_change_flavor_to_flavor_from_different_service(self):
        self.client.force_authenticate(user=self.fixture.admin)

        new_flavor = factories.FlavorFactory(disk=self.instance.disk + 1)

        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}

        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='change_flavor'), data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertDictContainsSubset({'flavor': ['New flavor is not within the same service settings']},
                                      response.data)

        reread_instance = Instance.objects.get(pk=self.instance.pk)

        self.assertEqual(reread_instance.disk, self.instance.disk,
                         'Instance disk not have changed')

    def test_user_cannot_change_flavor_of_instance_he_has_no_role_in(self):
        self.client.force_authenticate(user=self.fixture.admin)

        inaccessible_instance = factories.InstanceFactory()

        new_flavor = factories.FlavorFactory(
            settings=inaccessible_instance.service_project_link.service.settings,
            disk=self.instance.disk + 1,
        )

        data = {'flavor': factories.FlavorFactory.get_url(new_flavor)}

        response = self.client.post(factories.InstanceFactory.get_url(inaccessible_instance, action='change_flavor'), data)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        reread_instance = Instance.objects.get(pk=inaccessible_instance.pk)
        self.assertEqual(reread_instance.disk, inaccessible_instance.disk,
                         'Instance disk not have changed')

    def test_user_cannot_flavor_change_instance_in_creation_scheduled_state(self):
        self.client.force_authenticate(user=self.fixture.user)

        instance = factories.InstanceFactory(state=Instance.States.CREATION_SCHEDULED)
        project = instance.service_project_link.project
        project.add_user(self.fixture.user, ProjectRole.ADMINISTRATOR)

        response = self.client.post(factories.InstanceFactory.get_url(instance, action='change_flavor'), {})
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
            link = instance.service_project_link

            link.project.add_user(self.fixture.user, ProjectRole.ADMINISTRATOR)

            changed_flavor = factories.FlavorFactory(settings=link.service.settings)

            data = {'flavor': factories.FlavorFactory.get_url(changed_flavor)}

            response = self.client.post(factories.InstanceFactory.get_url(instance, action='change_flavor'), data)

            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

            reread_instance = Instance.objects.get(pk=instance.pk)
            self.assertEqual(reread_instance.disk, instance.disk,
                             'Instance disk not have changed')

    def test_user_cannot_flavor_change_with_empty_parameters(self):
        self.client.force_authenticate(user=self.fixture.user)

        instance = factories.InstanceFactory(
            state=Instance.States.OK,
            runtime_state=Instance.RuntimeStates.SHUTOFF,
        )
        project = instance.service_project_link.project

        project.add_user(self.fixture.user, ProjectRole.ADMINISTRATOR)

        data = {}

        response = self.client.post(factories.InstanceFactory.get_url(instance, action='change_flavor'), data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
