from django.db import transaction

from waldur_core.logging import tasks


def process_hook(sender, instance, created=False, **kwargs):
    transaction.on_commit(lambda: tasks.process_event.delay(instance.pk))
