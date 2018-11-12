import django_filters

from waldur_core.structure import filters as structure_filters

from . import models


class ImageFilter(structure_filters.BaseServicePropertyFilter):
    class Meta(structure_filters.BaseServicePropertyFilter.Meta):
        model = models.Image


class VirtualMachineFilter(structure_filters.BaseResourceFilter):
    external_ip = django_filters.CharFilter(name='public_ips')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.VirtualMachine
