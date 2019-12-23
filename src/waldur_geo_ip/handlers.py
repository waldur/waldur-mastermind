from django.conf import settings

from waldur_core.core import utils
from waldur_core.core.models import StateMixin

from . import tasks


def detect_vm_coordinates(sender, instance, name, source, target, **kwargs):
    # Check if geolocation is enabled
    if not settings.WALDUR_CORE.get('ENABLE_GEOIP', True):
        return

    # VM already has coordinates
    if instance.latitude is not None and instance.longitude is not None:
        return

    if target == StateMixin.States.OK:
        tasks.detect_vm_coordinates.delay(utils.serialize_instance(instance))
