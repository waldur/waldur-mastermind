import logging
from celery import shared_task

from waldur_core.core import utils as core_utils

from . import backend, exceptions

logger = logging.getLogger(__name__)


@shared_task
def create_doi(serialized_instance):
    instance = core_utils.deserialize_instance(serialized_instance)
    try:
        backend.DataciteBackend().create_doi(instance)
    except exceptions.DataciteException as e:
        logger.critical(e)
