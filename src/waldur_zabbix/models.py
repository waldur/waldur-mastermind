from __future__ import unicode_literals

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

from waldur_core.core.fields import JSONField
from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models

from . import managers


class ZabbixService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project, related_name='zabbix_services', through='ZabbixServiceProjectLink')

    @classmethod
    def get_url_name(cls):
        return 'zabbix'


class ZabbixServiceProjectLink(structure_models.ServiceProjectLink):
    service = models.ForeignKey(ZabbixService)

    @classmethod
    def get_url_name(cls):
        return 'zabbix-spl'


@python_2_unicode_compatible
class Host(structure_models.NewResource):
    VISIBLE_NAME_MAX_LENGTH = 64

    # list of items that are added as monitoring items to hosts scope.
    # parameters:
    #  zabbix_item_key - Zabbix item key,
    #  monitoring_item_name - name of monitoring item that will be attached to host scope,
    #  after_creation_update - True if monitoring item need to be updated frequently after host creation,
    #  after_creation_update_terminate_values - stop after_creation_update if monitoring item value is one of
    #                                           terminated values.
    MONITORING_ITEMS_CONFIGS = [
        {
            'zabbix_item_key': 'application.status',
            'monitoring_item_name': 'application_state',
            'after_creation_update': True,
            'after_creation_update_terminate_values': ['1'],
        }
    ]

    class Statuses(object):
        MONITORED = '0'
        UNMONITORED = '1'

        CHOICES = ((MONITORED, 'monitored'), (UNMONITORED, 'unmonitored'))

    service_project_link = models.ForeignKey(ZabbixServiceProjectLink, related_name='hosts', on_delete=models.PROTECT)
    visible_name = models.CharField(_('visible name'), max_length=VISIBLE_NAME_MAX_LENGTH)
    interface_parameters = JSONField(blank=True)
    host_group_name = models.CharField(_('host group name'), max_length=64, blank=True)
    error = models.CharField(max_length=500, blank=True, help_text='Error text if Zabbix agent is unavailable.')
    status = models.CharField(max_length=30, choices=Statuses.CHOICES, default=Statuses.MONITORED)
    templates = models.ManyToManyField('Template', related_name='hosts')

    content_type = models.ForeignKey(ContentType, null=True)
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey('content_type', 'object_id')

    objects = managers.HostManager('scope')

    def __str__(self):
        return '%s (%s)' % (self.name, self.visible_name)

    @classmethod
    def get_url_name(cls):
        return 'zabbix-host'

    def clean(self):
        # It is impossible to mark service and name unique together at DB level, because host is connected with service
        # through SPL.
        same_service_hosts = Host.objects.filter(service_project_link__service=self.service_project_link.service)
        if same_service_hosts.filter(name=self.name).exclude(pk=self.pk).exists():
            raise ValidationError(
                'Host with name "%s" already exists at this service. Host name should be unique.' % self.name)
        if same_service_hosts.filter(visible_name=self.visible_name).exclude(pk=self.pk).exists():
            raise ValidationError('Host with visible_name "%s" already exists at this service.'
                                  ' Host name should be unique.' % self.visible_name)

    @classmethod
    def get_visible_name_from_scope(cls, scope):
        """ Generate visible name based on host scope """
        return ('%s-%s' % (scope.uuid.hex, scope.name))[:64]


# Zabbix host name max length - 64
Host._meta.get_field('name').max_length = 64


class Template(structure_models.ServiceProperty):
    parents = models.ManyToManyField('Template', related_name='children')

    @classmethod
    def get_url_name(cls):
        return 'zabbix-template'


@python_2_unicode_compatible
class Item(models.Model):
    class ValueTypes:
        FLOAT = 0
        CHAR = 1
        LOG = 2
        INTEGER = 3
        TEXT = 4

        CHOICES = (
            (FLOAT, 'Numeric (float)'),
            (CHAR, 'Character'),
            (LOG, 'Log'),
            (INTEGER, 'Numeric (unsigned)'),
            (TEXT, 'Text')
        )

    key = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    template = models.ForeignKey(Template, related_name='items')
    backend_id = models.CharField(max_length=64)
    value_type = models.IntegerField(choices=ValueTypes.CHOICES)
    units = models.CharField(max_length=255)
    history = models.IntegerField()
    delay = models.IntegerField()

    def is_byte(self):
        return self.units == 'B'

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Trigger(structure_models.ServiceProperty):
    template = models.ForeignKey(Template, related_name='triggers')
    # https://www.zabbix.com/documentation/3.4/manual/api/reference/trigger/object
    priority = models.IntegerField(default=0)

    class Priority:
        DEFAULT = 0
        INFORMATION = 1
        WARNING = 2
        AVERAGE = 3
        HIGH = 4
        DISASTER = 5

        CHOICES = (
            (DEFAULT, 'Default'),
            (INFORMATION, 'Information'),
            (WARNING, 'Warning'),
            (AVERAGE, 'Average'),
            (HIGH, 'High'),
            (DISASTER, 'Disaster'),
        )

    class AcknowledgeStatus:
        SOME_EVENTS_UNACKNOWLEDGED = 1
        LAST_EVENT_UNACKNOWLEDGED = 2
        ALL_EVENTS_ACKNOWLEDGED = 3

        CHOICES = (
            (SOME_EVENTS_UNACKNOWLEDGED, 'Some events unacknowledged'),
            (LAST_EVENT_UNACKNOWLEDGED, 'Last event unacknowledged'),
            (ALL_EVENTS_ACKNOWLEDGED, 'All events unacknowledged'),
        )

    class Value:
        OK = 0
        PROBLEM = 1

        CHOICES = (
            (OK, 'OK'),
            (PROBLEM, 'Problem'),
        )

    @classmethod
    def get_url_name(cls):
        return 'zabbix-trigger'

    def __str__(self):
        return '%s-%s | %s' % (self.template.name, self.name, self.settings)


# Zabbix trigger name max length - 255
Trigger._meta.get_field('name').max_length = 255


class ITService(structure_models.NewResource):
    class Algorithm:
        SKIP = 0
        ANY = 1
        ALL = 2

        CHOICES = (
            (SKIP, 'do not calculate'),
            (ANY, 'problem, if at least one child has a problem'),
            (ALL, 'problem, if all children have problems')
        )

    service_project_link = models.ForeignKey(
        ZabbixServiceProjectLink, related_name='itservices', on_delete=models.PROTECT)
    host = models.ForeignKey(Host, related_name='itservices', blank=True, null=True)
    is_main = models.BooleanField(
        default=True, help_text='Main IT service SLA will be added to hosts resource as monitoring item.')

    algorithm = models.PositiveSmallIntegerField(choices=Algorithm.CHOICES, default=Algorithm.SKIP)
    sort_order = models.PositiveSmallIntegerField(default=1)
    agreed_sla = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)

    backend_trigger_id = models.CharField(max_length=64, null=True, blank=True)
    trigger = models.ForeignKey(Trigger, null=True, blank=True)

    class Meta(object):
        unique_together = ('host', 'is_main')

    @classmethod
    def get_url_name(cls):
        return 'zabbix-itservice'


@python_2_unicode_compatible
class SlaHistory(models.Model):
    itservice = models.ForeignKey(ITService)
    period = models.CharField(max_length=10)
    value = models.DecimalField(max_digits=11, decimal_places=4, null=True, blank=True)

    class Meta:
        verbose_name = 'SLA history'
        verbose_name_plural = 'SLA histories'
        unique_together = ('itservice', 'period')

    def __str__(self):
        return 'SLA for %s during %s: %s' % (self.itservice, self.period, self.value)


@python_2_unicode_compatible
class SlaHistoryEvent(models.Model):
    EVENTS = (
        ('U', 'DOWN'),
        ('D', 'UP'),
    )

    history = models.ForeignKey(SlaHistory, related_name='events')
    timestamp = models.IntegerField()
    state = models.CharField(max_length=1, choices=EVENTS)

    def __str__(self):
        return '%s - %s' % (self.timestamp, self.state)


class UserGroup(structure_models.ServiceProperty):
    @classmethod
    def get_url_name(cls):
        return 'zabbix-user-group'

    def get_backend(self):
        return self.settings.get_backend()


@python_2_unicode_compatible
class User(core_models.StateMixin, structure_models.ServiceProperty):
    class Types(object):
        DEFAULT = '1'
        ADMIN = '2'
        SUPERADMIN = '3'

        CHOICES = ((DEFAULT, 'default'), (ADMIN, 'admin'), (SUPERADMIN, 'superadmin'))

    alias = models.CharField(max_length=150)
    surname = models.CharField(max_length=150)
    type = models.CharField(max_length=30, choices=Types.CHOICES, default=Types.DEFAULT)
    groups = models.ManyToManyField(UserGroup, related_name='users')
    # password can be blank if user was pulled from zabbix, not created through NC
    password = models.CharField(max_length=150, blank=True)
    # phone is NC-only field
    phone = models.CharField(max_length=30, blank=True)

    class Meta(object):
        unique_together = ('alias', 'settings')

    def __str__(self):
        return '%s | %s' % (self.alias, self.settings)

    @classmethod
    def get_url_name(cls):
        return 'zabbix-user'

    def get_backend(self):
        return self.settings.get_backend()
