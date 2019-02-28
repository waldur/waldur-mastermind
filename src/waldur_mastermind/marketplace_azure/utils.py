import logging

from django.core.exceptions import ObjectDoesNotExist

from waldur_azure import models as azure_models
from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models


logger = logging.getLogger(__name__)


def synchronize_nic(nic):
    try:
        vm = azure_models.VirtualMachine.objects.get(network_interface=nic)
        resource = marketplace_models.Resource.objects.get(scope=vm)
    except ObjectDoesNotExist:
        logger.debug('Skipping Azure virtual machine synchronization '
                     'because marketplace resource does not exist. '
                     'Resource: %s', core_utils.serialize_instance(nic))
        return
    else:
        resource.backend_metadata['internal_ips'] = vm.internal_ips
        resource.backend_metadata['external_ips'] = vm.external_ips
        resource.save(update_fields=['backend_metadata'])
