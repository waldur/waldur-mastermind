import logging

from django.core.exceptions import ObjectDoesNotExist

from waldur_azure import models as azure_models
from waldur_core.core import utils as core_utils

from . import utils


logger = logging.getLogger(__name__)


def synchronize_nic(sender, instance, created=False, **kwargs):
    nic = instance
    if not created and not set(nic.tracker.changed()) & {'public_ip_id', 'ip_address'}:
        return

    utils.synchronize_nic(nic)


def synchronize_public_ip(sender, instance, created=False, **kwargs):
    public_ip = instance

    if not created and not public_ip.tracker.has_changed('ip_address'):
        return

    try:
        nic = azure_models.NetworkInterface.objects.get(public_ip=public_ip)
        utils.synchronize_nic(nic)
    except ObjectDoesNotExist:
        logger.debug('Skipping resource synchronization for Azure virtual machine'
                     'because marketplace resource does not exist. '
                     'Resource: %s', core_utils.serialize_instance(public_ip))
        return
