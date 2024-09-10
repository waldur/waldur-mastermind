import uuid

import factory
import pytz
from django.urls import reverse
from django.utils import timezone
from factory import fuzzy

from waldur_core.core.types import BaseMetaFactory
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.factories import ProjectFactory
from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack_tenant import models


class OpenStackTenantServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    class Meta:
        model = structure_models.ServiceSettings
        exclude = ("tenant",)

    name = factory.SelfAttribute("tenant.name")
    scope = factory.SelfAttribute("tenant")
    customer = factory.SelfAttribute("tenant.customer")
    backend_url = factory.SelfAttribute("tenant.service_settings.backend_url")
    username = factory.SelfAttribute("tenant.user_username")
    password = factory.SelfAttribute("tenant.user_password")
    type = "OpenStackTenant"
    tenant = factory.SubFactory(openstack_factories.TenantFactory)
    options = {"tenant_id": uuid.uuid4().hex}


class VolumeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Volume

    name = factory.Sequence(lambda n: "volume%s" % n)
    service_settings = factory.SubFactory(OpenStackTenantServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    size = 10 * 1024
    backend_id = factory.LazyAttribute(lambda _: uuid.uuid4().hex)

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = InstanceFactory()
        url = "http://testserver" + reverse(
            "openstacktenant-volume-detail", kwargs={"uuid": instance.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstacktenant-volume-list")
        return url if action is None else url + action + "/"


class InstanceAvailabilityZoneFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.InstanceAvailabilityZone

    name = factory.Sequence(lambda n: "instance_availability_zone_%s" % n)
    settings = factory.SubFactory(OpenStackTenantServiceSettingsFactory)

    @classmethod
    def get_url(cls, instance=None):
        if instance is None:
            instance = InstanceAvailabilityZoneFactory()
        return "http://testserver" + reverse(
            "openstacktenant-instance-availability-zone-detail",
            kwargs={"uuid": instance.uuid.hex},
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse(
            "openstacktenant-instance-availability-zone-list"
        )


class InstanceFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Instance]
):
    class Meta:
        model = models.Instance

    name = factory.Sequence(lambda n: "instance%s" % n)
    service_settings = factory.SubFactory(OpenStackTenantServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    backend_id = factory.Sequence(lambda n: "backend_id_%s" % n)
    server_group = factory.SubFactory(openstack_factories.ServerGroupFactory)
    ram = 2048

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = InstanceFactory()
        url = "http://testserver" + reverse(
            "openstacktenant-instance-detail", kwargs={"uuid": instance.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstacktenant-instance-list")
        return url if action is None else url + action + "/"

    @factory.post_generation
    def volumes(self, create, extracted, **kwargs):
        if not create:
            return

        self.volumes.create(
            backend_id=f"{self.name}-system",
            service_settings=self.service_settings,
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


class BackupScheduleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.BackupSchedule

    instance = factory.SubFactory(InstanceFactory)
    state = models.BackupSchedule.States.OK
    service_settings = factory.SelfAttribute("instance.service_settings")
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
            "openstacktenant-backup-schedule-detail", kwargs={"uuid": schedule.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstacktenant-backup-schedule-list")


class BackupFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Backup

    service_settings = factory.SubFactory(OpenStackTenantServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    backup_schedule = factory.SubFactory(BackupScheduleFactory)
    instance = factory.LazyAttribute(lambda b: b.backup_schedule.instance)
    state = models.Backup.States.OK
    kept_until = fuzzy.FuzzyDateTime(timezone.datetime(2017, 6, 6, tzinfo=pytz.UTC))

    @classmethod
    def get_url(cls, backup=None, action=None):
        if backup is None:
            backup = BackupFactory()
        url = "http://testserver" + reverse(
            "openstacktenant-backup-detail", kwargs={"uuid": backup.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstacktenant-backup-list")


class SnapshotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Snapshot

    size = 1024
    service_settings = factory.SubFactory(OpenStackTenantServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    source_volume = factory.SubFactory(VolumeFactory)
    name = factory.Sequence(lambda n: "Snapshot #%s" % n)
    state = models.Snapshot.States.OK

    @classmethod
    def get_url(cls, snapshot, action=None):
        if snapshot is None:
            snapshot = SnapshotFactory()
        url = "http://testserver" + reverse(
            "openstacktenant-snapshot-detail", kwargs={"uuid": snapshot.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("openstacktenant-snapshot-list")
        return url if action is None else url + action + "/"


class SnapshotRestorationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.SnapshotRestoration

    snapshot = factory.SubFactory(SnapshotFactory)
    volume = factory.SubFactory(VolumeFactory)


class SnapshotScheduleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.SnapshotSchedule

    source_volume = factory.SubFactory(VolumeFactory)
    state = models.SnapshotSchedule.States.OK
    service_settings = factory.SelfAttribute("source_volume.service_settings")
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
            "openstacktenant-snapshot-schedule-detail",
            kwargs={"uuid": schedule.uuid.hex},
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("openstacktenant-snapshot-schedule-list")


class VolumeAvailabilityZoneFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.VolumeAvailabilityZone

    name = factory.Sequence(lambda n: "volume_availability_zone_%s" % n)
    settings = factory.SubFactory(OpenStackTenantServiceSettingsFactory)

    @classmethod
    def get_url(cls, volume_availability_zone=None):
        if volume_availability_zone is None:
            volume_availability_zone = VolumeAvailabilityZoneFactory()
        return "http://testserver" + reverse(
            "openstacktenant-volume-availability-zone-detail",
            kwargs={"uuid": volume_availability_zone.uuid.hex},
        )

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse(
            "openstacktenant-volume-availability-zone-list"
        )
