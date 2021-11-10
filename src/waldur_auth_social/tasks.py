import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model

from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.utils import pull_remote_eduteams_user

User = get_user_model()

logger = logging.getLogger(__name__)


@shared_task(name='waldur_auth_social.pull_remote_eduteams_users')
def pull_remote_eduteams_users():
    if not settings.WALDUR_AUTH_SOCIAL['REMOTE_EDUTEAMS_ENABLED']:
        return
    for remote_user in User.objects.filter(registration_method='eduteams').order_by(
        'last_sync'
    )[:20]:
        try:
            pull_remote_eduteams_user(remote_user.username)
        except OAuthException:
            logger.exception(
                f'Unable to pull remote eduteams user {remote_user.username}'
            )
            continue
