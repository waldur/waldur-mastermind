import logging

from waldur_core.core import utils as core_utils

from . import mixins, tasks

logger = logging.getLogger(__name__)


def create_doi(instance):
    if isinstance(instance, mixins.DataciteMixin):
        serialized_instance = core_utils.serialize_instance(instance)
        tasks.create_doi.delay(serialized_instance)
    else:
        logger.warning("Instance %s is not DataciteMixin item." % instance)


def update_doi(instance):
    if isinstance(instance, mixins.DataciteMixin):
        serialized_instance = core_utils.serialize_instance(instance)
        tasks.update_pid.delay(serialized_instance)
    else:
        logger.warning("Instance %s is not DataciteMixin item." % instance)
