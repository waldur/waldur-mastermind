from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class ServiceProjectLinkFilter(structure_filters.BaseServiceProjectLinkFilter):
    service = core_filters.URLFilter(
        view_name='remote-waldur-detail', field_name='service__uuid'
    )

    class Meta(structure_filters.BaseServiceProjectLinkFilter.Meta):
        model = models.RemoteWaldurServiceProjectLink
