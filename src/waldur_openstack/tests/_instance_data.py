from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.tests import factories
from waldur_openstack.tests.fixtures import OpenStackFixture


def get_instance_data(fixture: OpenStackFixture):
    instance = fixture.instance
    tenant = instance.tenant
    factories.FloatingIPFactory(tenant=tenant, runtime_state="DOWN")
    ssh_public_key = structure_factories.SshPublicKeyFactory(user=fixture.admin)
    subnet = factories.SubNetFactory(tenant=tenant)
    return {
        "name": "test-host",
        "description": "test description",
        "flavor": factories.FlavorFactory.get_url(fixture.flavor),
        "image": factories.ImageFactory.get_url(fixture.image),
        "service_settings": factories.SettingsFactory.get_url(
            instance.service_settings
        ),
        "tenant": factories.TenantFactory.get_url(tenant),
        "project": structure_factories.ProjectFactory.get_url(instance.project),
        "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(
            ssh_public_key
        ),
        "system_volume_size": max(fixture.image.min_disk, 1024),
        "ports": [{"subnet": factories.SubNetFactory.get_url(subnet)}],
    }
