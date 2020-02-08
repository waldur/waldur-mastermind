import logging

from django.db import transaction

from . import tasks

logger = logging.getLogger(__name__)


def notify_create_user(sender, instance, password, created=False, **kwargs):
    transaction.on_commit(lambda: tasks.notify_create_user.delay(instance.id,
                                                                 password,
                                                                 instance.settings.backend_url))
