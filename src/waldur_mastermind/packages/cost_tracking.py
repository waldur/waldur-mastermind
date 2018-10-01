from __future__ import unicode_literals

import logging

from waldur_core.cost_tracking import CostTrackingStrategy, ConsumableItem, CostTrackingRegister
from waldur_openstack.openstack import models as openstack_models

from . import models, utils


logger = logging.getLogger(__name__)


class TenantStrategy(CostTrackingStrategy):
    resource_class = openstack_models.Tenant

    @classmethod
    def get_consumable_items(cls):
        for package_template in models.PackageTemplate.objects.all():
            yield utils.get_consumable_item(package_template)

    @classmethod
    def get_configuration(cls, tenant):
        configuration = {}
        if tenant.state != tenant.States.ERRED:
            if 'package_name' not in tenant.extra_configuration:
                logger.debug(
                    'Package name is not defined in configuration of tenant %s, (PK: %s)', tenant.name, tenant.pk)
            else:
                package_name = tenant.extra_configuration['package_name']
                configuration = {
                    ConsumableItem(item_type=utils.Types.PACKAGE_TEMPLATE, key=package_name): 1,
                }
        return configuration


CostTrackingRegister.register_strategy(TenantStrategy)
