import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model

from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.models import ProviderChoices
from waldur_auth_social.utils import pull_remote_eduteams_user
from waldur_core.core import models as core_models

from . import utils

User = get_user_model()

logger = logging.getLogger(__name__)


@shared_task(name='waldur_auth_social.pull_remote_eduteams_users')
def pull_remote_eduteams_users():
    if not settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_ENABLED']:
        return
    for remote_user in User.objects.filter(
        registration_method=ProviderChoices.EDUTEAMS
    ).order_by('last_sync')[:75]:
        try:
            pull_remote_eduteams_user(remote_user.username)
        except OAuthException:
            logger.exception(
                f'Unable to pull remote eduteams user {remote_user.username}'
            )
            continue


@shared_task(name='waldur_auth_social.pull_remote_eduteams_ssh_keys')
def pull_remote_eduteams_ssh_keys():
    if not settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_ENABLED']:
        return
    user_ssh_keys = utils.get_remote_eduteams_ssh_keys()
    for cuid, ssh_keys_map in user_ssh_keys.items():
        user = User.objects.filter(username=cuid).first()
        if user is None:
            logger.warning('There is no user with username %s', cuid)
            continue

        keys = ssh_keys_map['ssh_keys']
        for key in keys:
            existing_ssh_key = core_models.SshPublicKey.objects.filter(
                user=user, public_key=key
            ).first()
            if existing_ssh_key is None:
                new_key = core_models.SshPublicKey.objects.create(
                    user=user, public_key=key
                )
                logger.info('%s key is added to user %s', new_key.fingerprint)

        stale_keys = core_models.SshPublicKey.objects.filter(user=user).exclude(
            public_key__in=keys
        )
        if stale_keys:
            logger.info(
                'Deleting stale keys for user %s. Keys: ',
                cuid,
                ', '.join([key.fingerprint for key in stale_keys]),
            )
            stale_keys.delete()
