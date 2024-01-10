import uuid
from random import randint

import factory
from django.urls import reverse

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.factories import ProjectFactory
from waldur_openstack.openstack import models


class OpenStackServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    type = "OpenStack"


class FlavorFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Flavor

    name = factory.Sequence(lambda n: "flavor%s" % n)
    settings = factory.SubFactory(OpenStackServiceSettingsFactory)

    cores = 2
    ram = 2 * 1024
    disk = 10 * 1024

    backend_id = factory.Sequence(lambda n: "flavor-id%s" % n)

    @classmethod
    def get_url(cls, flavor=None):
        if flavor is None:
            flavor = FlavorFactory()
        return "http://testserver" + reverse(
            "openstack-flavor-detail", kwargs={"uuid": flavor.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-flavor-list")


class ImageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Image

    name = factory.Sequence(lambda n: "image%s" % n)
    settings = factory.SubFactory(OpenStackServiceSettingsFactory)

    backend_id = factory.Sequence(lambda n: "image-id%s" % n)

    @classmethod
    def get_url(cls, image=None):
        if image is None:
            image = ImageFactory()
        return "http://testserver" + reverse(
            "openstack-image-detail", kwargs={"uuid": image.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-image-list")


class TenantMixin:
    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        """Create an instance of the model, and save it to the database."""
        manager = cls._get_manager(model_class)

        if cls._meta.django_get_or_create:
            return cls._get_or_create(model_class, *args, **kwargs)

        if "tenant" not in kwargs:
            tenant, _ = models.Tenant.objects.get_or_create(
                service_settings=kwargs["service_settings"],
                project=kwargs["project"],
                backend_id="VALID_ID",
            )
            kwargs["tenant"] = tenant

        return manager.create(*args, **kwargs)


class SecurityGroupFactory(TenantMixin, factory.django.DjangoModelFactory):
    class Meta:
        model = models.SecurityGroup

    name = factory.Sequence(lambda n: "security_group%s" % n)
    service_settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    state = models.SecurityGroup.States.OK
    backend_id = factory.Sequence(lambda n: "security_group-id%s" % n)

    @classmethod
    def get_url(cls, sgp=None, action=None):
        if sgp is None:
            sgp = SecurityGroupFactory()
        url = "http://testserver" + reverse(
            "openstack-sgp-detail", kwargs={"uuid": sgp.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-sgp-list")


class SecurityGroupRuleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.SecurityGroupRule

    security_group = factory.SubFactory(SecurityGroupFactory)
    backend_id = factory.Sequence(lambda n: "security_group-rule-id%s" % n)
    protocol = models.SecurityGroupRule.TCP
    from_port = factory.fuzzy.FuzzyInteger(1, 30000)
    to_port = factory.fuzzy.FuzzyInteger(30000, 65535)
    cidr = factory.LazyAttribute(
        lambda o: ".".join("%s" % randint(1, 255) for i in range(4))  # noqa: S311
        + "/24"
    )


class FloatingIPFactory(TenantMixin, factory.django.DjangoModelFactory):
    class Meta:
        model = models.FloatingIP

    service_settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    runtime_state = factory.Iterator(["ACTIVE", "SHUTOFF", "DOWN"])
    address = factory.LazyAttribute(
        lambda o: ".".join("%s" % randint(0, 255) for _ in range(4))  # noqa: S311
    )
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = FloatingIPFactory()
        url = "http://testserver" + reverse(
            "openstack-fip-detail", kwargs={"uuid": instance.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-fip-list")


class TenantFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Tenant

    name = factory.Sequence(lambda n: "tenant%s" % n)
    service_settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    state = models.Tenant.States.OK
    external_network_id = factory.LazyAttribute(lambda _: uuid.uuid4())
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)

    user_username = factory.Sequence(lambda n: "tenant user%d" % n)
    user_password = core_utils.pwgen()

    @classmethod
    def get_url(cls, tenant=None, action=None):
        if tenant is None:
            tenant = TenantFactory()
        url = "http://testserver" + reverse(
            "openstack-tenant-detail", kwargs={"uuid": tenant.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstack-tenant-list")
        return url if action is None else url + action + "/"


class NetworkFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Network

    name = factory.Sequence(lambda n: "network%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id%s" % n)
    service_settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    tenant = factory.SubFactory(TenantFactory)
    state = models.Network.States.OK

    @classmethod
    def get_url(cls, network=None, action=None):
        if network is None:
            network = NetworkFactory()

        url = "http://testserver" + reverse(
            "openstack-network-detail", kwargs={"uuid": network.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-network-list")


class SubNetFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.SubNet

    name = factory.Sequence(lambda n: "subnet%s" % n)
    network = factory.SubFactory(NetworkFactory)
    service_settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)

    @classmethod
    def get_url(cls, subnet=None, action=None):
        if subnet is None:
            subnet = SubNetFactory()

        url = "http://testserver" + reverse(
            "openstack-subnet-detail", kwargs={"uuid": subnet.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-subnet-list")


class SharedOpenStackServiceSettingsFactory(OpenStackServiceSettingsFactory):
    shared = True


class CustomerOpenStackFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.CustomerOpenStack

    settings = factory.SubFactory(SharedOpenStackServiceSettingsFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)
    external_network_id = factory.LazyAttribute(lambda _: uuid.uuid4())


class VolumeTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.VolumeType

    name = factory.Sequence(lambda n: "volume_type_%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    settings = factory.SubFactory(OpenStackServiceSettingsFactory)

    @classmethod
    def get_url(cls, volume_type=None):
        if volume_type is None:
            volume_type = VolumeTypeFactory()
        return "http://testserver" + reverse(
            "openstack-volume-type-detail", kwargs={"uuid": volume_type.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-volume-type-list")


class PortFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Port

    name = factory.Sequence(lambda n: "port_%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    service_settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)

    @classmethod
    def get_url(cls, port=None):
        if port is None:
            port = PortFactory()
        return "http://testserver" + reverse(
            "openstack-port-detail", kwargs={"uuid": port.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-port-list")


class ServerGroupFactory(TenantMixin, factory.django.DjangoModelFactory):
    class Meta:
        model = models.ServerGroup

    name = factory.Sequence(lambda n: "server_group%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    policy = models.ServerGroup.AFFINITY
    state = models.ServerGroup.States.OK
    service_settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    tenant = factory.SubFactory(TenantFactory)

    @classmethod
    def get_url(cls, server_group=None, action=None):
        if server_group is None:
            server_group = ServerGroupFactory()
        url = "http://testserver" + reverse(
            "openstack-server-group-detail", kwargs={"uuid": server_group.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-server-group-list")


class RouterFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Router

    tenant = factory.SubFactory(TenantFactory)
    project = factory.SubFactory(ProjectFactory)
    service_settings = factory.SubFactory(OpenStackServiceSettingsFactory)
    name = factory.Sequence(lambda n: "router%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    state = models.Network.States.OK

    @classmethod
    def get_url(cls, router=None, action=None):
        if router is None:
            router = RouterFactory()

        url = "http://testserver" + reverse(
            "openstack-router-detail", kwargs={"uuid": router.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-router-list")
