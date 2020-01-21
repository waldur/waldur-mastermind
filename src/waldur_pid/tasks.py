from celery import shared_task

from waldur_core.core import utils as core_utils

from . import backend


@shared_task
def create_doi(serialized_instance):
    instance = core_utils.deserialize_instance(serialized_instance)
    backend.DataciteBackend().create_doi(instance)
