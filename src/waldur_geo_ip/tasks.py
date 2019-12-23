import logging

from celery import shared_task
from django.core import exceptions

from waldur_core.core import utils
from waldur_geo_ip import utils as geo_ip_utils

logger = logging.getLogger(__name__)


@shared_task(name='waldur_geo_ip.detect_vm_coordinates')
def detect_vm_coordinates(serialized_virtual_machine):

    try:
        vm = utils.deserialize_instance(serialized_virtual_machine)
    except exceptions.ObjectDoesNotExist:
        logger.warning('Missing virtual machine %s.', serialized_virtual_machine)
        return

    geo_ip_utils.detect_coordinates(vm)


@shared_task(name='waldur_geo_ip.detect_vm_coordinates_batch')
def detect_vm_coordinates_batch(serialized_virtual_machines):
    for vm in serialized_virtual_machines:
        detect_vm_coordinates.delay(vm)
