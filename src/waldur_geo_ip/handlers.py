from django.db import transaction

from waldur_core.core import utils
from waldur_core.core.models import StateMixin

from . import tasks


def detect_vm_coordinates(sender, instance, name, source, target, **kwargs):
    # VM already has coordinates
    if instance.latitude is not None and instance.longitude is not None:
        return

    if target == StateMixin.States.OK:
        transaction.on_commit(
            lambda: tasks.detect_vm_coordinates.delay(
                utils.serialize_instance(instance)
            )
        )


def detect_event_geo_location(sender, instance, created=False, **kwargs):
    event = instance

    if created:
        if (
            event.context.get("ip_address")
            and event.context.get("location") == "pending"
        ):
            transaction.on_commit(
                lambda: tasks.detect_event_location.delay(
                    utils.serialize_instance(event)
                )
            )
