import django_filters

from waldur_core.structure import filters as structure_filters

from . import models


class ImageFilter(structure_filters.BaseServicePropertyFilter):

    class Meta:
        model = models.Image
        fields = structure_filters.BaseServicePropertyFilter.Meta.fields + ('region',)

    region = django_filters.UUIDFilter(name='region__uuid')


class SizeFilter(structure_filters.BaseServicePropertyFilter):

    class Meta:
        model = models.Size
        fields = structure_filters.BaseServicePropertyFilter.Meta.fields + ('region',)

    region = django_filters.UUIDFilter(name='regions__uuid')


class RegionFilter(structure_filters.BaseServicePropertyFilter):

    class Meta(structure_filters.BaseServicePropertyFilter.Meta):
        model = models.Region


class InstanceFilter(structure_filters.BaseResourceFilter):
    external_ip = django_filters.CharFilter(name='public_ips')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Instance
