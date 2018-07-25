import logging
# patched with xmlrpc.monkey_patch() below
import xmlrpclib  # nosec

from celery import shared_task
from defusedxml import xmlrpc
from django.conf import settings
from django.core.validators import MaxValueValidator

from . import models

xmlrpc.monkey_patch()
logger = logging.getLogger(__name__)


@shared_task(name='waldur_ansible.sync_pip_libraries')
def sync_pip_libraries():
    """
    This task is used by Celery beat in order to periodically
    schedule available PIP libraries synchronization.
    """
    schedule_sync()


def schedule_sync():
    """
    This function calls task only if it is enabled. It is optionally enabled due to its heavy processing nature.
    """
    if not settings.WALDUR_PYTHON_MANAGEMENT.get('SYNC_PIP_PACKAGES_TASK_ENABLED'):
        return

    _sync_pip_libraries.apply_async(countdown=10)


@shared_task()
def _sync_pip_libraries():
    """
    This task is called asynchronously by Celery beat schedule.
    """
    logger.info('Started synching PIP packages.')
    client = xmlrpclib.ServerProxy('https://pypi.python.org/pypi')
    actual_repository_packages = client.list_packages()
    previously_cached_packages = models.CachedRepositoryPythonLibrary.objects.values_list('name', flat=True)

    delete_removed_libraries(actual_repository_packages, previously_cached_packages)

    persist_new_libraries(actual_repository_packages, previously_cached_packages)


def persist_new_libraries(actual_repository_packages, previously_cached_packages):
    current_batch = []
    for library_name in actual_repository_packages:
        if library_name not in previously_cached_packages:
            if len(current_batch) == settings.WALDUR_PYTHON_MANAGEMENT.get('SYNC_PIP_PACKAGES_BATCH_SIZE'):
                try:
                    models.CachedRepositoryPythonLibrary.objects.bulk_create(current_batch)
                except MaxValueValidator:
                    logger.exception('Pip backend could not save "%s" python library: name is too long.', library_name)
                current_batch = []

            current_batch.append(models.CachedRepositoryPythonLibrary(name=library_name))

    if current_batch:
        models.CachedRepositoryPythonLibrary.objects.bulk_create(current_batch)


def delete_removed_libraries(actual_repository_packages, previously_cached_packages):
    library_names_to_delete = []
    for previously_cached_package in previously_cached_packages:
        if previously_cached_package not in actual_repository_packages:
            library_names_to_delete.append(previously_cached_package)
    if library_names_to_delete:
        models.CachedRepositoryPythonLibrary.objects.filter(name__in=library_names_to_delete).delete()
