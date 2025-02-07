from constance import config
from django.core.cache import cache

QUOTA_NAME = "freeipa_quota"

CACHE_KEY = "waldur_freeipa_syncing_groups"


def is_syncing():
    """
    This function checks if task is already running.
    """
    return cache.get(CACHE_KEY)


def renew_task_status():
    """
    This function sets lock with timeout. Lock is valid only for 1 minute.
    Then it should be renewed. Otherwise, lock is released.
    """
    cache.set(CACHE_KEY, True, 60)


def release_task_status():
    cache.set(CACHE_KEY, False)


def generate_username(username):
    # Prepend username suffix
    prefix = config.FREEIPA_USERNAME_PREFIX
    if prefix:
        username = f"{prefix}{username}"
    return username.lower()
