from mock import patch
from rest_framework import test, status

from waldur_core.structure.tests import factories as structure_factories

from .. import models
from . import factories, fixtures


def _instance_data(user, instance=None):
    if instance is None:
        instance = factories.InstanceFactory()
    factories.FloatingIPFactory(settings=instance.service_project_link.service.settings, runtime_state='DOWN')
    image = factories.ImageFactory(settings=instance.service_project_link.service.settings)
    flavor = factories.FlavorFactory(settings=instance.service_project_link.service.settings)
    ssh_public_key = structure_factories.SshPublicKeyFactory(user=user)
    subnet = factories.SubNetFactory(settings=instance.service_project_link.service.settings)
    return {
        'name': 'test_host',
        'description': 'test description',
        'flavor': factories.FlavorFactory.get_url(flavor),
        'image': factories.ImageFactory.get_url(image),
        'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(
            instance.service_project_link),
        'ssh_public_key': structure_factories.SshPublicKeyFactory.get_url(ssh_public_key),
        'system_volume_size': max(image.min_disk, 1024),
        'internal_ips_set': [{'subnet': factories.SubNetFactory.get_url(subnet)}],
    }


class InstanceSecurityGroupsTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance
        self.settings = self.fixture.openstack_tenant_service_settings
        self.admin = self.fixture.admin
        self.client.force_authenticate(self.admin)

        self.security_groups = factories.SecurityGroupFactory.create_batch(2, settings=self.settings)
        self.instance.security_groups.add(*self.security_groups)

    def test_groups_list_in_instance_response(self):
        response = self.client.get(factories.InstanceFactory.get_url(self.instance))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        fields = ('name',)
        for field in fields:
            expected_security_groups = [getattr(g, field) for g in self.security_groups]
            self.assertItemsEqual([g[field] for g in response.data['security_groups']], expected_security_groups)

    def test_add_instance_with_security_groups(self):
        data = _instance_data(self.admin, self.instance)
        data['security_groups'] = [self._get_valid_security_group_payload(sg)
                                   for sg in self.security_groups]

        response = self.client.post(factories.InstanceFactory.get_list_url(), data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        reread_instance = models.Instance.objects.get(pk=self.instance.pk)
        reread_security_groups = list(reread_instance.security_groups.all())
        self.assertEquals(reread_security_groups, self.security_groups)

    @patch('waldur_openstack.openstack_tenant.executors.InstanceUpdateSecurityGroupsExecutor.execute')
    def test_change_instance_security_groups_single_field(self, mocked_execute_method):
        new_security_group = factories.SecurityGroupFactory(
            name='test-group',
            settings=self.settings,
        )

        data = {
            'security_groups': [
                self._get_valid_security_group_payload(new_security_group),
            ]
        }

        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='update_security_groups'),
                                    data=data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        reread_instance = models.Instance.objects.get(pk=self.instance.pk)
        reread_security_groups = list(reread_instance.security_groups.all())

        self.assertEquals(reread_security_groups, [new_security_group],
                          'Security groups should have changed')
        mocked_execute_method.assert_called_once()

    @patch('waldur_openstack.openstack_tenant.executors.InstanceUpdateSecurityGroupsExecutor.execute')
    def test_change_instance_security_groups(self, mocked_execute_method):
        response = self.client.get(factories.InstanceFactory.get_url(self.instance))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        security_group = factories.SecurityGroupFactory(settings=self.settings)
        data = {'security_groups': [self._get_valid_security_group_payload(security_group)]}

        response = self.client.post(factories.InstanceFactory.get_url(self.instance, action='update_security_groups'),
                                    data=data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        reread_instance = models.Instance.objects.get(pk=self.instance.pk)
        reread_security_groups = list(reread_instance.security_groups.all())

        self.assertEquals(reread_security_groups, [security_group])
        mocked_execute_method.assert_called_once()

    def test_security_groups_is_not_required(self):
        data = _instance_data(self.admin, self.instance)
        self.assertNotIn('security_groups', data)
        response = self.client.post(factories.InstanceFactory.get_list_url(), data=data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # Helper methods
    def _get_valid_security_group_payload(self, security_group=None):
        return {'url': factories.SecurityGroupFactory.get_url(security_group)}
