from waldur_core.cost_tracking import CostTrackingStrategy, ConsumableItem, CostTrackingRegister

from . import models, backend


class AzureCostTrackingStrategy(CostTrackingStrategy):
    resource_class = models.VirtualMachine

    class Types(object):
        FLAVOR = 'flavor'

    @classmethod
    def get_consumable_items(cls):
        return [ConsumableItem(item_type=cls.Types.FLAVOR, key=size.name, default_price=size.price)
                for size in backend.SizeQueryset()]

    @classmethod
    def get_configuration(cls, virtual_machine):
        consumables = {}
        if virtual_machine.state != models.VirtualMachine.States.ERRED and virtual_machine.image_name:
            consumables[ConsumableItem(item_type=cls.Types.FLAVOR, key=virtual_machine.image_name)] = 1
        return consumables


CostTrackingRegister.register_strategy(AzureCostTrackingStrategy)
