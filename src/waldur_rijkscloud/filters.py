from __future__ import unicode_literals

import django_filters
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class ServiceProjectLinkFilter(structure_filters.BaseServiceProjectLinkFilter):
    service = core_filters.URLFilter(view_name='rijkscloud-detail', name='service__uuid')

    class Meta(structure_filters.BaseServiceProjectLinkFilter.Meta):
        model = models.RijkscloudServiceProjectLink


class FlavorFilter(structure_filters.ServicePropertySettingsFilter):

    o = django_filters.OrderingFilter(fields=('cores', 'ram'))

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Flavor


class VolumeFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Volume


class InstanceFilter(structure_filters.BaseResourceFilter):
    external_ip = django_filters.CharFilter(name='floating_ip__address')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Instance
        fields = structure_filters.BaseResourceFilter.Meta.fields + ('runtime_state',)


class NetworkFilter(structure_filters.ServicePropertySettingsFilter):

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Network


class SubNetFilter(structure_filters.ServicePropertySettingsFilter):
    network = core_filters.URLFilter(view_name='rijkscloud-network-detail', name='network__uuid')
    network_uuid = django_filters.UUIDFilter(name='network__uuid')

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.SubNet


class InternalIPFilter(structure_filters.ServicePropertySettingsFilter):
    is_available = django_filters.BooleanFilter(widget=BooleanWidget)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.InternalIP
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'address', 'subnet')


class FloatingIPFilter(structure_filters.ServicePropertySettingsFilter):
    is_available = django_filters.BooleanFilter(widget=BooleanWidget)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.FloatingIP
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'address',)
