from waldur_core.core.managers import GenericKeyMixin
from waldur_core.structure.managers import StructureManager
from waldur_core.structure.models import BaseResource


def filter_active(qs):
    INVALID_STATES = (
        BaseResource.States.CREATION_SCHEDULED,
        BaseResource.States.DELETION_SCHEDULED,
        BaseResource.States.DELETING,
        BaseResource.States.ERRED,
    )
    return qs.exclude(backend_id='', state__in=INVALID_STATES)


class HostManager(GenericKeyMixin, StructureManager):
    """ Allows to filter and get hosts by generic key """

    def get_available_models(self):
        """ Return list of models that are acceptable """
        return BaseResource.get_all_models()
