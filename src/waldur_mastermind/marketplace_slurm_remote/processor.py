from django.db import transaction

from waldur_mastermind.marketplace import processors


class CreateAllocationProcessor(processors.BasicCreateResourceProcessor):
    pass


class DeleteAllocationProcessor(processors.BasicDeleteResourceProcessor):
    def process_order(self, user):
        with transaction.atomic():
            marketplace_resource = self.order.resource
            marketplace_resource.set_state_terminating()
            marketplace_resource.save(update_fields=["state"])


class UpdateAllocationLimitsProcessor(processors.BasicUpdateResourceProcessor):
    pass
