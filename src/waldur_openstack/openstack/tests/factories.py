from random import randint
import uuid

from django.urls import reverse
import factory

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories

from .. import models


class OpenStackServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    type = 'OpenStack'


class OpenStackServiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.OpenStackService

    settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = OpenStackServiceFactory()
        url = 'http://testserver' + reverse('openstack-detail', kwargs={'uuid': service.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('openstack-list')


class OpenStackServiceProjectLinkFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.OpenStackServiceProjectLink

    service = factory.SubFactory(OpenStackServiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, spl=None, action=None):
        if spl is None:
            spl = OpenStackServiceProjectLinkFactory()
        url = 'http://testserver' + reverse('openstack-spl-detail', kwargs={'pk': spl.pk})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('openstack-spl-list')


class FlavorFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Flavor

    name = factory.Sequence(lambda n: 'flavor%s' % n)
    settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)

    cores = 2
    ram = 2 * 1024
    disk = 10 * 1024

    backend_id = factory.Sequence(lambda n: 'flavor-id%s' % n)

    @classmethod
    def get_url(cls, flavor=None):
        if flavor is None:
            flavor = FlavorFactory()
        return 'http://testserver' + reverse('openstack-flavor-detail', kwargs={'uuid': flavor.uuid})

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('openstack-flavor-list')


class ImageFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Image

    name = factory.Sequence(lambda n: 'image%s' % n)
    settings = factory.SubFactory(structure_factories.ServiceSettingsFactory)

    backend_id = factory.Sequence(lambda n: 'image-id%s' % n)

    @classmethod
    def get_url(cls, image=None):
        if image is None:
            image = ImageFactory()
        return 'http://testserver' + reverse('openstack-image-detail', kwargs={'uuid': image.uuid})

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('openstack-image-list')


class TenantMixin(object):
    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        """Create an instance of the model, and save it to the database."""
        manager = cls._get_manager(model_class)

        if cls._meta.django_get_or_create:
            return cls._get_or_create(model_class, *args, **kwargs)

        if 'tenant' not in kwargs:
            tenant, _ = models.Tenant.objects.get_or_create(
                service_project_link=kwargs['service_project_link'],
                backend_id='VALID_ID')
            kwargs['tenant'] = tenant

        return manager.create(*args, **kwargs)


class SecurityGroupFactory(TenantMixin, factory.DjangoModelFactory):
    class Meta(object):
        model = models.SecurityGroup

    name = factory.Sequence(lambda n: 'security_group%s' % n)
    service_project_link = factory.SubFactory(OpenStackServiceProjectLinkFactory)
    state = models.SecurityGroup.States.OK
    backend_id = factory.Sequence(lambda n: 'security_group-id%s' % n)

    @classmethod
    def get_url(cls, sgp=None, action=None):
        if sgp is None:
            sgp = SecurityGroupFactory()
        url = 'http://testserver' + reverse('openstack-sgp-detail', kwargs={'uuid': sgp.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('openstack-sgp-list')


class SecurityGroupRuleFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.SecurityGroupRule

    security_group = factory.SubFactory(SecurityGroupFactory)
    protocol = models.SecurityGroupRule.TCP
    from_port = factory.fuzzy.FuzzyInteger(1, 30000)
    to_port = factory.fuzzy.FuzzyInteger(30000, 65535)
    cidr = factory.LazyAttribute(lambda o: '.'.join('%s' % randint(1, 255) for i in range(4)) + '/24')


class FloatingIPFactory(TenantMixin, factory.DjangoModelFactory):
    class Meta(object):
        model = models.FloatingIP

    service_project_link = factory.SubFactory(OpenStackServiceProjectLinkFactory)
    runtime_state = factory.Iterator(['ACTIVE', 'SHUTOFF', 'DOWN'])
    address = factory.LazyAttribute(lambda o: '.'.join('%s' % randint(0, 255) for _ in range(4)))
    backend_id = factory.Sequence(lambda n: 'backend_id_%s' % n)

    @classmethod
    def get_url(cls, instance=None):
        if instance is None:
            instance = FloatingIPFactory()
        return 'http://testserver' + reverse('openstack-fip-detail', kwargs={'uuid': instance.uuid})

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('openstack-fip-list')


class TenantFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Tenant

    name = factory.Sequence(lambda n: 'tenant%s' % n)
    service_project_link = factory.SubFactory(OpenStackServiceProjectLinkFactory)
    state = models.Tenant.States.OK
    external_network_id = factory.LazyAttribute(lambda _: uuid.uuid4())
    backend_id = factory.Sequence(lambda n: 'backend_id_%s' % n)

    user_username = factory.Sequence(lambda n: 'tenant user%d' % n)
    user_password = core_utils.pwgen()

    @classmethod
    def get_url(cls, tenant=None, action=None):
        if tenant is None:
            tenant = TenantFactory()
        url = 'http://testserver' + reverse('openstack-tenant-detail', kwargs={'uuid': tenant.uuid.hex})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls, action=None):
        url = 'http://testserver' + reverse('openstack-tenant-list')
        return url if action is None else url + action + '/'


class NetworkFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Network

    name = factory.Sequence(lambda n: 'network%s' % n)
    backend_id = factory.Sequence(lambda n: 'backend_id%s' % n)
    service_project_link = factory.SubFactory(OpenStackServiceProjectLinkFactory)
    tenant = factory.SubFactory(TenantFactory)
    state = models.Network.States.OK

    @classmethod
    def get_url(cls, network=None, action=None):
        if network is None:
            network = NetworkFactory()

        url = 'http://testserver' + reverse('openstack-network-detail', kwargs={'uuid': network.uuid.hex})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('openstack-network-list')


class SubNetFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.SubNet

    name = factory.Sequence(lambda n: 'subnet%s' % n)
    network = factory.SubFactory(NetworkFactory)
    service_project_link = factory.SubFactory(OpenStackServiceProjectLinkFactory)

    @classmethod
    def get_url(cls, subnet=None, action=None):
        if subnet is None:
            subnet = SubNetFactory()

        url = 'http://testserver' + reverse('openstack-subnet-detail', kwargs={'uuid': subnet.uuid.hex})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('openstack-subnet-list')


class SharedOpenStackServiceSettingsFactory(OpenStackServiceSettingsFactory):
    shared = True


class CustomerOpenStackFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.CustomerOpenStack

    settings = factory.SubFactory(SharedOpenStackServiceSettingsFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)
    external_network_id = factory.LazyAttribute(lambda _: uuid.uuid4())
