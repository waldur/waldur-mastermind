import logging

from celery import shared_task
from django.conf import settings

from .backend import KeycloakBackend

logger = logging.getLogger(__name__)


@shared_task(name='waldur_keycloak.sync_groups')
def sync_groups():
    if not settings.WALDUR_KEYCLOAK['ENABLED']:
        logger.debug('Skipping Keycloak synchronization because plugin is disabled.')
        return

    KeycloakBackend().synchronize_groups()
