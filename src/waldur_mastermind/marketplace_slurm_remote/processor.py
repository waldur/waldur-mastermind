from django.db import transaction

from waldur_mastermind.marketplace import processors
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_slurm import models as slurm_models


class CreateAllocationProcessor(processors.BasicCreateResourceProcessor):
    def process_order_item(self, user):
        with transaction.atomic():
            marketplace_resource = marketplace_utils.create_local_resource(
                self.order_item, None
            )
            allocation = slurm_models.Allocation.objects.create(
                name=self.order_item.attributes['name'],
                service_settings=self.order_item.offering.scope,
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
