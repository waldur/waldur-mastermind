from django_filters import OrderingFilter

from waldur_core.structure import filters as structure_filters

from . import models


class ImageFilter(structure_filters.BaseServicePropertyFilter):

    o = OrderingFilter(fields=('distribution', 'type'))

    class Meta(object):
        model = models.Image
        fields = structure_filters.BaseServicePropertyFilter.Meta.fields + ('distribution', 'type')


class SizeFilter(structure_filters.BaseServicePropertyFilter):

    class Meta(object):
        model = models.Size
        fields = structure_filters.BaseServicePropertyFilter.Meta.fields + ('cores', 'ram', 'disk')


class RegionFilter(structure_filters.BaseServicePropertyFilter):
    class Meta(structure_filters.BaseServicePropertyFilter.Meta):
        model = models.Region


class DropletFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Droplet
