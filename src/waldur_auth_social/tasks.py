import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q

from waldur_auth_social.const import ProviderChoices
from waldur_auth_social.exceptions import OAuthException
from waldur_auth_social.utils import pull_remote_eduteams_user, sync_user_ssh_keys
from waldur_core.core.models import User

from . import utils

logger = logging.getLogger(__name__)


@shared_task(name="waldur_auth_social.pull_remote_eduteams_users")
def pull_remote_eduteams_users():
    if not settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_ENABLED"]:
        return
    for remote_user in User.objects.filter(
        Q(identity_source=ProviderChoices.EDUTEAMS)
        | Q(registration_method=ProviderChoices.EDUTEAMS)
    ).order_by("last_sync")[:75]:
        try:
            pull_remote_eduteams_user(remote_user.username)
        except OAuthException:
            logger.exception(
                f"Unable to pull remote eduteams user {remote_user.username}"
            )
            continue


@shared_task(name="waldur_auth_social.pull_remote_eduteams_ssh_keys")
def pull_remote_eduteams_ssh_keys():
    if not settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_ENABLED"]:
        return

    user_ssh_keys = utils.get_remote_eduteams_ssh_keys()
    if not user_ssh_keys:
        return

    remote_user_usernames = set(user_ssh_keys.keys())
    users_by_username = {
        user.username: user
        for user in User.objects.filter(username__in=remote_user_usernames)
    }
    missing_users = remote_user_usernames - set(users_by_username.keys())
    logger.info("Number of missing users in SSH key sync: %s", len(missing_users))
    for username, user in users_by_username.items():
        keys = user_ssh_keys[username]["ssh_keys"]
        sync_user_ssh_keys(user, keys, username)


@shared_task(name="waldur_auth_social.rotate_remote_eduteams_token")
def rotate_remote_eduteams_token():
    if not settings.WALDUR_AUTH_SOCIAL["REMOTE_EDUTEAMS_ENABLED"]:
        return
    try:
        utils.refresh_remote_eduteams_token(force=True)
        logger.info("Proactive eduTEAMS token rotation completed successfully.")
    except Exception:
        logger.exception(
            "Failed to rotate eduTEAMS refresh token. "
            "The token may expire if this persists."
        )
