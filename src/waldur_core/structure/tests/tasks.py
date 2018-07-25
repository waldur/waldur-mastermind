from celery import shared_task

from waldur_core.core import utils as core_utils


@shared_task
def pull_instance(serialized_instance, pulled_disk):
    """ Test-only task that allows to emulate pull operation """
    instance = core_utils.deserialize_instance(serialized_instance)
    instance.disk = pulled_disk
    instance.save()
