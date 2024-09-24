from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.models import Tenant
from waldur_openstack.tests import factories


def get_instance_data(user, instance=None):
    if instance is None:
        instance = factories.InstanceFactory()
    tenant: Tenant = instance.tenant
    factories.FloatingIPFactory(tenant=tenant, runtime_state="DOWN")
    image = factories.ImageFactory(settings=tenant.service_settings)
    flavor = factories.FlavorFactory(settings=tenant.service_settings)
    ssh_public_key = structure_factories.SshPublicKeyFactory(user=user)
    subnet = factories.SubNetFactory(tenant=tenant)
    return {
        "name": "test-host",
        "description": "test description",
        "flavor": factories.FlavorFactory.get_url(flavor),
        "image": factories.ImageFactory.get_url(image),
        "service_settings": factories.SettingsFactory.get_url(
            instance.service_settings
        ),
        "tenant": factories.TenantFactory.get_url(tenant),
        "project": structure_factories.ProjectFactory.get_url(instance.project),
        "ssh_public_key": structure_factories.SshPublicKeyFactory.get_url(
            ssh_public_key
        ),
        "system_volume_size": max(image.min_disk, 1024),
        "ports": [{"subnet": factories.SubNetFactory.get_url(subnet)}],
    }
