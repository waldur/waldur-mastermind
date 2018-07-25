from __future__ import unicode_literals

from ddt import ddt, data
from mock import patch
from rest_framework import status
from rest_framework import test

from waldur_core.core.tests import helpers
from waldur_core.structure.tests import factories as structure_factories

from .. import models
from . import factories, fixtures


@ddt
class BackupDeleteTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()

    @data('staff', 'owner', 'manager', 'admin')
    def test_user_can_delete_backup(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.BackupFactory.get_url(self.fixture.backup)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data('global_support', 'customer_support')
    def test_user_can_not_delete_backup(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.BackupFactory.get_url(self.fixture.backup)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class BackupListPermissionsTest(helpers.ListPermissionsTest):

    def get_url(self):
        return factories.BackupFactory.get_list_url()

    def get_users_and_expected_results(self):
        models.Backup.objects.all().delete()
        instance = factories.InstanceFactory()
        backup1 = factories.BackupFactory(instance=instance)
        backup2 = factories.BackupFactory(instance=instance)

        user_with_view_permission = structure_factories.UserFactory.create(is_staff=True, is_superuser=True)
        user_without_view_permission = structure_factories.UserFactory.create()

        return [
            {
                'user': user_with_view_permission,
                'expected_results': [
                    {'url': factories.BackupFactory.get_url(backup1)},
                    {'url': factories.BackupFactory.get_url(backup2)}
                ]
            },
            {
                'user': user_without_view_permission,
                'expected_results': []
            },
        ]


class BackupPermissionsTest(helpers.PermissionsTest):

    def setUp(self):
        super(BackupPermissionsTest, self).setUp()
        self.fixture = fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance
        self.backup = factories.BackupFactory(
            service_project_link=self.fixture.spl,
            state=models.Backup.States.OK,
            instance=self.instance,
        )

    def get_users_with_permission(self, url, method):
        if method == 'GET':
            return [self.fixture.staff, self.fixture.admin, self.fixture.manager]
        else:
            return [self.fixture.staff, self.fixture.admin, self.fixture.manager, self.fixture.owner]

    def get_users_without_permissions(self, url, method):
        return [self.fixture.user]

    def get_urls_configs(self):
        yield {'url': factories.BackupFactory.get_url(self.backup), 'method': 'GET'}
        yield {'url': factories.BackupFactory.get_url(self.backup), 'method': 'DELETE'}

    def test_permissions(self):
        with patch('waldur_openstack.openstack_tenant.executors.BackupDeleteExecutor.execute'):
            super(BackupPermissionsTest, self).test_permissions()


class BackupSourceFilterTest(test.APITransactionTestCase):

    def test_filter_backup_by_scope(self):
        user = structure_factories.UserFactory.create(is_staff=True)

        instance1 = factories.InstanceFactory()
        factories.BackupFactory(instance=instance1)
        factories.BackupFactory(instance=instance1)

        instance2 = factories.InstanceFactory()
        factories.BackupFactory(instance=instance2)

        self.client.force_authenticate(user=user)
        response = self.client.get(factories.BackupFactory.get_list_url())
        self.assertEqual(3, len(response.data))

        response = self.client.get(factories.BackupFactory.get_list_url(), data={
            'instance_uuid': instance1.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(2, len(response.data))
        self.assertEqual(factories.InstanceFactory.get_url(instance1), response.data[0]['instance'])


class BackupRestorationTest(test.APITransactionTestCase):

    def setUp(self):
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=user)

        self.backup = factories.BackupFactory(state=models.Backup.States.OK)
        self.url = factories.BackupFactory.get_url(self.backup, 'restore')

        system_volume = self.backup.instance.volumes.get(bootable=True)
        self.disk_size = system_volume.size

        self.service_settings = self.backup.instance.service_project_link.service.settings
        self.service_settings.options = {'external_network_id': 'id'}
        self.service_settings.save()
        self.valid_flavor = factories.FlavorFactory(disk=self.disk_size + 10, settings=self.service_settings)
        self.subnet = factories.SubNetFactory(settings=self.service_settings)

    def test_flavor_disk_size_should_match_system_volume_size(self):
        response = self.client.post(self.url, self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_if_flavor_disk_size_lesser_then_system_volume_size_validation_fails(self):
        invalid_flavor = factories.FlavorFactory(disk=self.disk_size - 10, settings=self.service_settings)
        response = self.client.post(self.url, self._get_valid_payload(
            flavor=factories.FlavorFactory.get_url(invalid_flavor))
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(response.data['flavor'], ['Flavor disk size should match system volume size.'])

    def test_security_groups_cannot_be_associated_if_they_belong_to_another_settings(self):
        security_group = factories.SecurityGroupFactory()
        self.assertNotEqual(self.backup.service_project_link.service.settings, security_group.settings)
        payload = self._get_valid_payload(
            security_groups=[{'url': factories.SecurityGroupFactory.get_url(security_group)}])

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('security_groups', response.data)

    def test_security_group_has_been_associated_with_an_instance(self):
        security_group1 = factories.SecurityGroupFactory(settings=self.service_settings)
        payload = self._get_valid_payload(
            security_groups=[{'url': factories.SecurityGroupFactory.get_url(security_group1)}])

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(response.data['security_groups'])
        self.assertEqual(response.data['security_groups'][0]['name'], security_group1.name)

    def test_floating_ip_is_not_associated_with_an_instance_if_it_is_booked_already(self):
        floating_ip = factories.FloatingIPFactory(is_booked=True, runtime_state='DOWN', settings=self.service_settings)
        subnet = factories.SubNetFactory(settings=self.service_settings)
        payload = self._get_valid_payload(
            floating_ips=[
                {'url': factories.FloatingIPFactory.get_url(floating_ip),
                 'subnet': factories.SubNetFactory.get_url(subnet)}],
            internal_ips_set=[{'subnet': factories.SubNetFactory.get_url(subnet)}])

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('floating_ips', response.data)

    def test_floating_ip_is_not_associated_with_an_instance_if_it_belongs_to_different_settings(self):
        floating_ip = factories.FloatingIPFactory(runtime_state='DOWN')
        self.assertNotEqual(self.service_settings, floating_ip.settings)
        subnet = factories.SubNetFactory(settings=self.service_settings)
        payload = self._get_valid_payload(
            floating_ips=[
                {'url': factories.FloatingIPFactory.get_url(floating_ip),
                 'subnet': factories.SubNetFactory.get_url(subnet)}],
            internal_ips_set=[{'subnet': factories.SubNetFactory.get_url(subnet)}])

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('floating_ips', response.data)

    def test_floating_ip_is_associated_with_an_instance_if_floating_ip_is_in_DOWN_state(self):
        floating_ip = factories.FloatingIPFactory(settings=self.service_settings, runtime_state='ACTIVE')
        subnet = factories.SubNetFactory(settings=self.service_settings)
        payload = self._get_valid_payload(
            floating_ips=[
                {'url': factories.FloatingIPFactory.get_url(floating_ip),
                 'subnet': factories.SubNetFactory.get_url(subnet)}],
            internal_ips_set=[{'subnet': factories.SubNetFactory.get_url(subnet)}])

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('floating_ips', response.data)

    def test_floating_ip_is_not_valid_if_it_is_already_assigned(self):
        subnet = factories.SubNetFactory(settings=self.service_settings)
        internal_ip = factories.InternalIPFactory(subnet=subnet)
        floating_ip = factories.FloatingIPFactory(
            internal_ip=internal_ip,
            settings=self.service_settings,
            runtime_state='ACTIVE',
        )

        payload = self._get_valid_payload(
            floating_ips=[
                {'url': factories.FloatingIPFactory.get_url(floating_ip),
                 'subnet': factories.SubNetFactory.get_url(subnet)}],
            internal_ips_set=[{'subnet': factories.SubNetFactory.get_url(subnet)}])

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('floating_ips', response.data)

    def test_floating_ip_is_not_associated_with_an_instance_if_subnet_is_not_connected_to_the_instance(self):
        floating_ip = factories.FloatingIPFactory(settings=self.service_settings, runtime_state='DOWN')
        subnet = factories.SubNetFactory(settings=self.service_settings)
        payload = self._get_valid_payload(
            floating_ips=[
                {'url': factories.FloatingIPFactory.get_url(floating_ip),
                 'subnet': factories.SubNetFactory.get_url(subnet)}])

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('floating_ips', response.data)

    def test_floating_ip_is_associated_with_an_instance(self):
        floating_ip = factories.FloatingIPFactory(settings=self.service_settings, runtime_state='DOWN')
        payload = self._get_valid_payload(
            floating_ips=[
                {'url': factories.FloatingIPFactory.get_url(floating_ip),
                 'subnet': factories.SubNetFactory.get_url(self.subnet)}],
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(response.data['floating_ips'])
        self.assertEqual(response.data['floating_ips'][0]['uuid'], floating_ip.uuid.hex)
        instance = models.Instance.objects.get(name=payload['name'])
        self.assertEqual(instance.floating_ips.count(), 1)
        self.assertEqual(instance.floating_ips.first().uuid.hex, floating_ip.uuid.hex)

    def test_internal_ips_are_not_associated_with_instance_if_subnet_belongs_to_another_settings(self):
        subnet = factories.SubNetFactory()
        payload = self._get_valid_payload(internal_ips_set=[{'subnet': factories.SubNetFactory.get_url(subnet)}])

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('internal_ips_set', response.data)

    def test_internal_ips_have_been_associated_with_instance(self):
        payload = self._get_valid_payload()

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(name=payload['name'])
        self.assertEqual(instance.internal_ips_set.count(), 1)
        self.assertEqual(instance.subnets.count(), 1)
        self.assertEqual(instance.subnets.first().uuid.hex, self.subnet.uuid.hex)
        self.assertEqual(instance.flavor_name, self.valid_flavor.name)

    def test_backup_can_be_restored_for_instance_with_1_volume(self):
        self.backup.instance.volumes.get(bootable=False).delete()
        payload = self._get_valid_payload()

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.BackupRestoration.objects.filter(instance__name=payload['name']).exists)

    def _get_valid_payload(self, **options):
        payload = {
            'name': 'instance name',
            'flavor': factories.FlavorFactory.get_url(self.valid_flavor),
            'internal_ips_set': [{'subnet': factories.SubNetFactory.get_url(self.subnet)}],
        }
        payload.update(options)
        return payload
