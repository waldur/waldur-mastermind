from django.core.cache import cache


def is_syncing(cache_key):
    """
    This function checks if task is already running.
    """
    return cache.get(cache_key)


def renew_task_status(cache_key, timeout):
    """
    This function sets lock with timeout. Lock is valid only for 10 minutes.
    Then it should be renewed. Otherwise, lock is released.
    """
    cache.set(cache_key, True, timeout)


def release_task_status(cache_key):
    cache.set(cache_key, False)
