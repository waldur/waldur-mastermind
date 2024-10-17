import uuid
from random import randint

import factory
import pytz
from django.urls import reverse
from django.utils import timezone
from factory import fuzzy

from waldur_core.core import utils as core_utils
from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import models


class SettingsFactory(structure_factories.ServiceSettingsFactory):
    type = "OpenStack"


class FlavorFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Flavor]
):
    class Meta:
        model = models.Flavor

    name = factory.Sequence(lambda n: "flavor%s" % n)
    settings = factory.SubFactory(SettingsFactory)

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
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstack-flavor-list")
        return url if action is None else url + action + "/"


class ImageFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Image]
):
    class Meta:
        model = models.Image

    name = factory.Sequence(lambda n: "image%s" % n)
    settings = factory.SubFactory(SettingsFactory)

    backend_id = factory.Sequence(lambda n: "image-id%s" % n)

    @classmethod
    def get_url(cls, image=None):
        if image is None:
            image = ImageFactory()
        return "http://testserver" + reverse(
            "openstack-image-detail", kwargs={"uuid": image.uuid.hex}
        )

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstack-image-list")
        return url if action is None else url + action + "/"


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


class SecurityGroupFactory(
    TenantMixin,
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.SecurityGroup],
):
    class Meta:
        model = models.SecurityGroup

    name = factory.Sequence(lambda n: "security_group%s" % n)
    service_settings = factory.SubFactory(SettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
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


class SecurityGroupRuleFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.SecurityGroupRule],
):
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


class FloatingIPFactory(
    TenantMixin,
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.FloatingIP],
):
    class Meta:
        model = models.FloatingIP

    service_settings = factory.SubFactory(SettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
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


class TenantFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Tenant]
):
    class Meta:
        model = models.Tenant

    name = factory.Sequence(lambda n: "tenant%s" % n)
    service_settings = factory.SubFactory(SettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
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


class NetworkFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Network]
):
    class Meta:
        model = models.Network

    name = factory.Sequence(lambda n: "network%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id%s" % n)
    service_settings = factory.SubFactory(SettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
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


class SubNetFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.SubNet]
):
    class Meta:
        model = models.SubNet

    name = factory.Sequence(lambda n: "subnet%s" % n)
    network = factory.SubFactory(NetworkFactory)
    service_settings = factory.SubFactory(SettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    tenant = factory.SubFactory(TenantFactory)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)

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


class SharedOpenStackServiceSettingsFactory(SettingsFactory):
    shared = True


class CustomerOpenStackFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.CustomerOpenStack],
):
    class Meta:
        model = models.CustomerOpenStack

    settings = factory.SubFactory(SharedOpenStackServiceSettingsFactory)
    customer = factory.SubFactory(structure_factories.CustomerFactory)
    external_network_id = factory.LazyAttribute(lambda _: uuid.uuid4())


class VolumeTypeFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.VolumeType]
):
    class Meta:
        model = models.VolumeType

    name = factory.Sequence(lambda n: "volume_type_%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    settings = factory.SubFactory(SettingsFactory)

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


class PortFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Port]
):
    class Meta:
        model = models.Port

    name = factory.Sequence(lambda n: "port_%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    service_settings = factory.SubFactory(SettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    tenant = factory.SubFactory(TenantFactory)

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


class ServerGroupFactory(
    TenantMixin,
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ServerGroup],
):
    class Meta:
        model = models.ServerGroup

    name = factory.Sequence(lambda n: "server_group%s" % n)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    policy = models.ServerGroup.AFFINITY
    state = models.ServerGroup.States.OK
    service_settings = factory.SubFactory(SettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
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


class RouterFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Router]
):
    class Meta:
        model = models.Router

    service_settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
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


class VolumeFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Volume]
):
    class Meta:
        model = models.Volume

    name = factory.Sequence(lambda n: "volume%s" % n)
    service_settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    size = 10 * 1024
    backend_id = factory.LazyAttribute(lambda _: uuid.uuid4().hex)

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = InstanceFactory()
        url = "http://testserver" + reverse(
            "openstack-volume-detail", kwargs={"uuid": instance.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstack-volume-list")
        return url if action is None else url + action + "/"


class InstanceAvailabilityZoneFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.InstanceAvailabilityZone],
):
    class Meta:
        model = models.InstanceAvailabilityZone

    name = factory.Sequence(lambda n: "instance_availability_zone_%s" % n)
    settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)

    @classmethod
    def get_url(cls, instance=None):
        if instance is None:
            instance = InstanceAvailabilityZoneFactory()
        return "http://testserver" + reverse(
            "openstack-instance-availability-zone-detail",
            kwargs={"uuid": instance.uuid.hex},
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse(
            "openstack-instance-availability-zone-list"
        )


class InstanceFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.Instance],
):
    class Meta:
        model = models.Instance

    name = factory.Sequence(lambda n: "instance%s" % n)
    service_settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    server_group = factory.SubFactory(ServerGroupFactory)
    ram = 2048

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = InstanceFactory()
        url = "http://testserver" + reverse(
            "openstack-instance-detail", kwargs={"uuid": instance.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstack-instance-list")
        return url if action is None else url + action + "/"

    @factory.post_generation
    def volumes(self, create, extracted, **kwargs):
        if not create:
            return

        self.volumes.create(
            backend_id=f"{self.name}-system",
            service_settings=self.service_settings,
            tenant=self.tenant,
            project=self.project,
            bootable=True,
            size=10 * 1024,
            name=f"{self.name}-system",
            image_name=f"{self.name}-image-name"
            if not kwargs
            else kwargs["image_name"],
        )
        self.volumes.create(
            backend_id=f"{self.name}-data",
            service_settings=self.service_settings,
            tenant=self.tenant,
            project=self.project,
            size=20 * 1024,
            name=f"{self.name}-data",
            state=models.Volume.States.OK,
        )

    @factory.post_generation
    def security_groups(self, create, extracted, **kwargs):
        if not create:
            return

        if extracted:
            for group in extracted:
                self.security_groups.add(group)


class BackupScheduleFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.BackupSchedule]
):
    class Meta:
        model = models.BackupSchedule

    instance = factory.SubFactory(InstanceFactory)
    state = models.BackupSchedule.States.OK
    service_settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)
    project = factory.SelfAttribute("instance.project")
    retention_time = 10
    is_active = True
    maximal_number_of_resources = 3
    schedule = "0 * * * *"

    @classmethod
    def get_url(cls, schedule, action=None):
        if schedule is None:
            schedule = BackupScheduleFactory()
        url = "http://testserver" + reverse(
            "openstack-backup-schedule-detail", kwargs={"uuid": schedule.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-backup-schedule-list")


class BackupFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Backup]
):
    class Meta:
        model = models.Backup

    service_settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    backup_schedule = factory.SubFactory(BackupScheduleFactory)
    instance = factory.LazyAttribute(lambda b: b.backup_schedule.instance)
    state = models.Backup.States.OK
    kept_until = fuzzy.FuzzyDateTime(timezone.datetime(2017, 6, 6, tzinfo=pytz.UTC))

    @classmethod
    def get_url(cls, backup=None, action=None):
        if backup is None:
            backup = BackupFactory()
        url = "http://testserver" + reverse(
            "openstack-backup-detail", kwargs={"uuid": backup.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-backup-list")


class SnapshotFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Snapshot]
):
    class Meta:
        model = models.Snapshot

    size = 1024
    service_settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    source_volume = factory.SubFactory(VolumeFactory)
    name = factory.Sequence(lambda n: "Snapshot #%s" % n)
    state = models.Snapshot.States.OK

    @classmethod
    def get_url(cls, snapshot, action=None):
        if snapshot is None:
            snapshot = SnapshotFactory()
        url = "http://testserver" + reverse(
            "openstack-snapshot-detail", kwargs={"uuid": snapshot.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstack-snapshot-list")
        return url if action is None else url + action + "/"


class SnapshotRestorationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.SnapshotRestoration

    snapshot = factory.SubFactory(SnapshotFactory)
    volume = factory.SubFactory(VolumeFactory)


class SnapshotScheduleFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.SnapshotSchedule],
):
    class Meta:
        model = models.SnapshotSchedule

    source_volume = factory.SubFactory(VolumeFactory)
    state = models.SnapshotSchedule.States.OK
    service_settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)
    project = factory.SelfAttribute("source_volume.project")
    retention_time = 10
    is_active = True
    maximal_number_of_resources = 3
    schedule = "0 * * * *"

    @classmethod
    def get_url(cls, schedule, action=None):
        if schedule is None:
            schedule = SnapshotScheduleFactory()
        url = "http://testserver" + reverse(
            "openstack-snapshot-schedule-detail",
            kwargs={"uuid": schedule.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-snapshot-schedule-list")


class VolumeAvailabilityZoneFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.VolumeAvailabilityZone],
):
    class Meta:
        model = models.VolumeAvailabilityZone

    name = factory.Sequence(lambda n: f"volume_availability_zone_{n}")
    settings = factory.LazyAttribute(lambda o: o.tenant.service_settings)
    tenant = factory.SubFactory(TenantFactory)

    @classmethod
    def get_url(cls, volume_availability_zone=None):
        if volume_availability_zone is None:
            volume_availability_zone = VolumeAvailabilityZoneFactory()
        return "http://testserver" + reverse(
            "openstack-volume-availability-zone-detail",
            kwargs={"uuid": volume_availability_zone.uuid.hex},
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstack-volume-availability-zone-list")
