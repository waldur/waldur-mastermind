from __future__ import unicode_literals

from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models


class VMwareService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project,
        related_name='+',
        through='VMwareServiceProjectLink'
    )

    class Meta:
        unique_together = ('customer', 'settings')
        verbose_name = _('VMware provider')
        verbose_name_plural = _('VMware providers')

    @classmethod
    def get_url_name(cls):
        return 'vmware'


class VMwareServiceProjectLink(structure_models.ServiceProjectLink):

    service = models.ForeignKey(VMwareService)

    class Meta(structure_models.ServiceProjectLink.Meta):
        verbose_name = _('VMware provider project link')
        verbose_name_plural = _('VMware provider project links')

    @classmethod
    def get_url_name(cls):
        return 'vmware-spl'


class VirtualMachineMixin(models.Model):
    class Meta:
        abstract = True

    guest_os = models.CharField(max_length=50, help_text=_('Defines the valid guest operating system '
                                                           'types used for configuring a virtual machine'))
    cores = models.PositiveSmallIntegerField(default=0, help_text=_('Number of cores in a VM'))
    cores_per_socket = models.PositiveSmallIntegerField(default=1, help_text=_('Number of cores in a VM'))
    ram = models.PositiveIntegerField(default=0, help_text=_('Memory size in MiB'), verbose_name=_('RAM'))
    disk = models.PositiveIntegerField(default=0, help_text=_('Disk size in MiB'))


@python_2_unicode_compatible
class VirtualMachine(VirtualMachineMixin,
                     core_models.RuntimeStateMixin,
                     structure_models.NewResource):
    service_project_link = models.ForeignKey(
        VMwareServiceProjectLink,
        related_name='+',
        on_delete=models.PROTECT
    )

    class RuntimeStates(object):
        POWERED_OFF = 'POWERED_OFF'
        POWERED_ON = 'POWERED_ON'
        SUSPENDED = 'SUSPENDED'

        CHOICES = (
            (POWERED_OFF, 'Powered off'),
            (POWERED_ON, 'Powered on'),
            (SUSPENDED, 'Suspended'),
        )

    class GuestPowerStates(object):
        RUNNING = 'RUNNING'
        SHUTTING_DOWN = 'SHUTTING_DOWN'
        RESETTING = 'RESETTING'
        STANDBY = 'STANDBY'
        NOT_RUNNING = 'NOT_RUNNING'
        UNAVAILABLE = 'UNAVAILABLE'

        CHOICES = (
            (RUNNING, 'Running'),
            (SHUTTING_DOWN, 'Shutting down'),
            (RESETTING, 'Resetting'),
            (STANDBY, 'Standby'),
            (NOT_RUNNING, 'Not running'),
            (UNAVAILABLE, 'Unavailable'),
        )

    template = models.ForeignKey('Template', null=True, on_delete=models.SET_NULL)
    cluster = models.ForeignKey('Cluster', null=True, on_delete=models.SET_NULL)
    datastore = models.ForeignKey('Datastore', null=True, on_delete=models.SET_NULL)
    folder = models.ForeignKey('Folder', null=True, on_delete=models.SET_NULL)
    networks = models.ManyToManyField('Network', blank=True)
    guest_power_enabled = models.BooleanField(
        default=False,
        help_text='Flag indicating if the virtual machine is ready to process soft power operations.'
    )
    guest_power_state = models.CharField(
        'The power state of the guest operating system.',
        max_length=150,
        blank=True,
        choices=GuestPowerStates.CHOICES,
    )
    tracker = FieldTracker()

    @classmethod
    def get_backend_fields(cls):
        return super(VirtualMachine, cls).get_backend_fields() + (
            'runtime_state', 'cores', 'cores_per_socket', 'ram', 'disk',
            'guest_power_state', 'guest_power_enabled'
        )

    @classmethod
    def get_url_name(cls):
        return 'vmware-virtual-machine'

    @property
    def total_disk(self):
        return self.disks.aggregate(models.Sum('size'))['size__sum'] or 0

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Port(core_models.RuntimeStateMixin, structure_models.NewResource):
    service_project_link = models.ForeignKey(
        VMwareServiceProjectLink,
        related_name='+',
        on_delete=models.PROTECT
    )
    vm = models.ForeignKey(VirtualMachine)
    network = models.ForeignKey('Network')
    mac_address = models.CharField(max_length=32, blank=True, verbose_name=_('MAC address'))

    @classmethod
    def get_backend_fields(cls):
        return super(Port, cls).get_backend_fields() + ('name', 'mac_address')

    @classmethod
    def get_url_name(cls):
        return 'vmware-port'

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Disk(structure_models.NewResource):
    service_project_link = models.ForeignKey(
        VMwareServiceProjectLink,
        related_name='+',
        on_delete=models.PROTECT
    )

    size = models.PositiveIntegerField(help_text=_('Size in MiB'))
    vm = models.ForeignKey(VirtualMachine, related_name='disks')

    @classmethod
    def get_url_name(cls):
        return 'vmware-disk'

    def __str__(self):
        return self.name

    @classmethod
    def get_backend_fields(cls):
        return super(Disk, cls).get_backend_fields() + ('name', 'size')


@python_2_unicode_compatible
class Template(VirtualMachineMixin,
               core_models.DescribableMixin,
               structure_models.ServiceProperty):
    created = models.DateTimeField()
    modified = models.DateTimeField()

    @classmethod
    def get_url_name(cls):
        return 'vmware-template'

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Cluster(structure_models.ServiceProperty):
    @classmethod
    def get_url_name(cls):
        return 'vmware-cluster'

    def __str__(self):
        return '%s / %s' % (self.settings, self.name)


class CustomerCluster(models.Model):
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    cluster = models.ForeignKey('Cluster', on_delete=models.CASCADE)

    def __str__(self):
        return '%s / %s' % (self.customer, self.cluster)

    class Meta(object):
        unique_together = ('customer', 'cluster')


@python_2_unicode_compatible
class Network(structure_models.ServiceProperty):
    type = models.CharField(max_length=255)

    @classmethod
    def get_url_name(cls):
        return 'vmware-network'

    def __str__(self):
        return '%s / %s' % (self.settings, self.name)


class CustomerNetwork(models.Model):
    # This model allows to specify allowed networks for VM provision
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    network = models.ForeignKey('Network', on_delete=models.CASCADE)

    def __str__(self):
        return '%s / %s' % (self.customer, self.network)

    class Meta(object):
        unique_together = ('customer', 'network')


class CustomerNetworkPair(models.Model):
    # This model allows to specify allowed networks for existing VM NIC provision
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    network = models.ForeignKey('Network', on_delete=models.CASCADE)

    def __str__(self):
        return '%s / %s' % (self.customer, self.network)

    class Meta(object):
        unique_together = ('customer', 'network')


@python_2_unicode_compatible
class Datastore(structure_models.ServiceProperty):
    type = models.CharField(max_length=255)
    capacity = models.PositiveIntegerField(help_text="Capacity, in MB.", null=True, blank=True)
    free_space = models.PositiveIntegerField(help_text="Available space, in MB.", null=True, blank=True)

    @classmethod
    def get_url_name(cls):
        return 'vmware-datastore'

    def __str__(self):
        return '%s / %s' % (self.settings, self.name)


class CustomerDatastore(models.Model):
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    datastore = models.ForeignKey('Datastore', on_delete=models.CASCADE)

    def __str__(self):
        return '%s / %s' % (self.customer, self.datastore)

    class Meta(object):
        unique_together = ('customer', 'datastore')


@python_2_unicode_compatible
class Folder(structure_models.ServiceProperty):

    def __str__(self):
        return '%s / %s' % (self.settings, self.name)

    @classmethod
    def get_url_name(cls):
        return 'vmware-folder'


class CustomerFolder(models.Model):
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    folder = models.ForeignKey('Folder', on_delete=models.CASCADE)

    def __str__(self):
        return '%s / %s' % (self.customer, self.folder)

    class Meta(object):
        unique_together = ('customer', 'folder')
