from __future__ import unicode_literals

from django.db import transaction

from . import tasks


def create_screenshot_thumbnail(sender, instance, created=False, **kwargs):
    if not created:
        return

    transaction.on_commit(lambda: tasks.create_screenshot_thumbnail.delay(instance.uuid))


def notifications_order_approval(sender, instance, created=False, **kwargs):
    if not created:
        return

    transaction.on_commit(lambda: tasks.notify_order_approvers.delay(instance.uuid))
