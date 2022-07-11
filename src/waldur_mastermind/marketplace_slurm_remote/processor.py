from django.db import transaction

from waldur_core.structure.models import ServiceSettings
from waldur_mastermind.marketplace import processors
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_slurm import models as slurm_models

from .apps import MarketplaceSlurmConfig


class CreateAllocationProcessor(processors.BasicCreateResourceProcessor):
    def process_order_item(self, user):
        with transaction.atomic():
            marketplace_resource = marketplace_utils.create_local_resource(
                self.order_item, None
            )

            service_settings, _ = ServiceSettings.objects.get_or_create(
                type=MarketplaceSlurmConfig.service_name,
                state=ServiceSettings.States.OK,
                shared=True,
                defaults={'name': 'SLURM remote service settings'},
            )

            allocation = slurm_models.Allocation.objects.create(
                name=self.order_item.attributes['name'],
                service_settings=service_settings,
                project=self.order_item.order.project,
            )
            marketplace_resource.scope = allocation
            marketplace_resource.save()


class DeleteAllocationProcessor(processors.BasicDeleteResourceProcessor):
    def process_order_item(self, user):
        with transaction.atomic():
            marketplace_resource = self.order_item.resource
            marketplace_resource.set_state_terminating()
            marketplace_resource.save(update_fields=['state'])

            allocation: slurm_models.Allocation = marketplace_resource.scope
            allocation.schedule_deleting()
            allocation.save(update_fields=['state'])


class UpdateAllocationLimitsProcessor(processors.BasicUpdateResourceProcessor):
    def update_limits_process(self, user):
        allocation: slurm_models.Allocation = self.order_item.resource.scope
        allocation.schedule_updating()
        allocation.save(update_fields=['state'])

        return False
