from django.core.cache import cache

QUOTA_NAME = 'freeipa_quota'

CACHE_KEY = 'waldur_freeipa_syncing_groups'


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


def get_names(full_name):
    full_name_list = full_name.split()
    initials = ''

    if full_name_list:
        first_name = full_name_list[0]
        initials = first_name[0]
    else:
        first_name = 'N/A'

    if len(full_name_list) > 1:
        last_name = full_name_list[-1]
        initials += last_name[0]
    else:
        last_name = 'N/A'
    return first_name, last_name, initials
