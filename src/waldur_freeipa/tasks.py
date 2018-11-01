import logging

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from python_freeipa import exceptions as freeipa_exceptions

from waldur_core.core import models as core_models

from . import models, utils
from .backend import FreeIPABackend


logger = logging.getLogger(__name__)


def schedule_sync():
    """
    This function calls task only if it is not already running.
    The goal is to avoid race conditions during concurrent task execution.
    """
    if utils.is_syncing():
        logger.debug('Skipping FreeIPA synchronization because synchronization is already in progress.')
        return

    if not settings.WALDUR_FREEIPA['ENABLED']:
        logger.debug('Skipping FreeIPA synchronization because plugin is disabled.')
        return

    utils.renew_task_status()
    _sync_groups.apply_async(countdown=10)


@shared_task(name='waldur_freeipa.sync_groups')
def sync_groups():
    """
    This task is used by Celery beat in order to periodically
    schedule FreeIPA group synchronization.
    """
    schedule_sync()


@shared_task()
def _sync_groups():
    """
    This task actually calls backend. It is called asynchronously
    either by signal handler or Celery beat schedule.
    """
    FreeIPABackend().synchronize_groups()


def schedule_sync_names():
    _sync_names.apply_async(countdown=10)


@shared_task()
def _sync_names():
    FreeIPABackend().synchronize_names()


def schedule_sync_gecos():
    _sync_gecos.apply_async(countdown=10)


@shared_task()
def _sync_gecos():
    FreeIPABackend().synchronize_gecos()


@shared_task(name='waldur_freeipa.sync_ssh_key')
def sync_ssh_key(key_id):
    try:
        ssh_key = core_models.SshPublicKey.objects.get(id=key_id)
    except ObjectDoesNotExist:
        logger.debug('Skipping SSH key synchronization because key has been deleted. '
                     'Key ID: %s', key_id)
        return

    try:
        profile = models.Profile.objects.get(user=ssh_key.user)
    except ObjectDoesNotExist:
        logger.debug('Skipping SSH key synchronization because '
                     'FreeIPA profile does not exist. '
                     'User ID: %s', ssh_key.user.id)
        return

    try:
        FreeIPABackend().update_ssh_keys(profile)
    except freeipa_exceptions.NotFound:
        logger.warning('Skipping SSH key synchronization because '
                       'FreeIPA profile has been removed on backend. '
                       'User ID: %s', ssh_key.user.id)
        return
