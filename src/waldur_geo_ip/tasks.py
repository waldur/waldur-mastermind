import logging

from celery import shared_task
from django.core import exceptions

from waldur_core.core import utils as core_utils
from waldur_geo_ip import utils

logger = logging.getLogger(__name__)


@shared_task(name='waldur_geo_ip.detect_vm_coordinates')
def detect_vm_coordinates(serialized_virtual_machine):

    try:
        vm = core_utils.deserialize_instance(serialized_virtual_machine)
    except exceptions.ObjectDoesNotExist:
        logger.warning('Missing virtual machine %s.', serialized_virtual_machine)
        return

    utils.detect_coordinates(vm)


@shared_task(name='waldur_geo_ip.detect_vm_coordinates_batch')
def detect_vm_coordinates_batch(serialized_virtual_machines):
    for vm in serialized_virtual_machines:
        detect_vm_coordinates.delay(vm)


@shared_task()
def detect_event_location(serialized_event):
    event = core_utils.deserialize_instance(serialized_event)
    location = ''

    try:
        location = utils.get_country_by_ip(event.context['ip_address'])
    except Exception as msg:
        logger.error('Error of get location info: %s', msg)

    event.context['location'] = location
    event.save(update_fields=['context'])
