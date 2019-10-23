from django_filters import OrderingFilter, CharFilter

from waldur_core.structure import filters as structure_filters

from . import models


class ImageFilter(structure_filters.BaseServicePropertyFilter):

    o = OrderingFilter(fields=('distribution', 'type'))

    class Meta:
        model = models.Image
        fields = structure_filters.BaseServicePropertyFilter.Meta.fields + ('distribution', 'type')


class SizeFilter(structure_filters.BaseServicePropertyFilter):

    class Meta:
        model = models.Size
        fields = structure_filters.BaseServicePropertyFilter.Meta.fields + ('cores', 'ram', 'disk')


class RegionFilter(structure_filters.BaseServicePropertyFilter):
    class Meta(structure_filters.BaseServicePropertyFilter.Meta):
        model = models.Region


class DropletFilter(structure_filters.BaseResourceFilter):
    external_ip = CharFilter(field_name='ip_address')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Droplet
