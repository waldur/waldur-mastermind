from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.common import utils as common_utils
from waldur_openstack.openstack_tenant import models, views
from waldur_openstack.openstack_tenant.tests import factories, fixtures


def _instance_data(user, instance=None):
    if instance is None:
        instance = factories.InstanceFactory()
    factories.FloatingIPFactory(
        settings=instance.service_settings, runtime_state='DOWN'
    )
    image = factories.ImageFactory(settings=instance.service_settings)
    flavor = factories.FlavorFactory(settings=instance.service_settings)
    ssh_public_key = structure_factories.SshPublicKeyFactory(user=user)
    subnet = factories.SubNetFactory(settings=instance.service_settings)
    return {
        'name': 'test-host',
        'description': 'test description',
        'flavor': factories.FlavorFactory.get_url(flavor),
        'image': factories.ImageFactory.get_url(image),
        'service_settings': factories.OpenStackTenantServiceSettingsFactory.get_url(
            instance.service_settings
        ),
        'project': structure_factories.ProjectFactory.get_url(instance.project),
        'ssh_public_key': structure_factories.SshPublicKeyFactory.get_url(
            ssh_public_key
        ),
        'system_volume_size': max(image.min_disk, 1024),
        'internal_ips_set': [{'subnet': factories.SubNetFactory.get_url(subnet)}],
    }


class InstanceServerGroupTest(test.APITransactionTestCase):
    def setUp(self):
        fixture = fixtures.OpenStackTenantFixture()
        self.instance = fixture.instance
        self.settings = fixture.openstack_tenant_service_settings
        self.admin = fixture.admin
        self.client.force_authenticate(self.admin)

        self.server_group = factories.ServerGroupFactory.create(settings=self.settings)
        self.instance.server_group = self.server_group
        self.instance.save()

    def create_instance(self, post_data=None):
        user = self.admin
        view = views.MarketplaceInstanceViewSet.as_view({'post': 'create'})
        response = common_utils.create_request(view, user, post_data)
        return response

    def test_server_group_in_instance_response(self):
        response = self.client.get(factories.InstanceFactory.get_url(self.instance))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        expected = getattr(self.server_group, 'name')
        actual = response.data['server_group']['name']
        self.assertEqual(expected, actual)

    def test_add_instance_with_server_group(self):
        data = _instance_data(self.admin, self.instance)
        data['server_group'] = self._get_valid_server_group_payload(self.server_group)

        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        reread_instance = models.Instance.objects.get(pk=self.instance.pk)
        reread_server_group = reread_instance.server_group
        self.assertEquals(reread_server_group, self.server_group)

    def test_server_group_is_not_required(self):
        data = _instance_data(self.admin, self.instance)
        self.assertNotIn('server_group', data)
        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def _get_valid_server_group_payload(self, server_group=None):
        return {'url': factories.ServerGroupFactory.get_url(server_group)}
