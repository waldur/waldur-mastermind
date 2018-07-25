from waldur_core.cost_tracking import CostTrackingRegister, CostTrackingStrategy, ConsumableItem

from . import models


class DropletStrategy(CostTrackingStrategy):
    resource_class = models.Droplet

    class Types(object):
        FLAVOR = 'flavor'

    @classmethod
    def get_consumable_items(cls):
        return [ConsumableItem(item_type=cls.Types.FLAVOR, key=size.name, default_price=size.price)
                for size in models.Size.objects.all()]

    @classmethod
    def get_configuration(cls, droplet):
        consumables = {}
        if droplet.state != models.Droplet.States.ERRED and droplet.size_name:
            consumables[ConsumableItem(item_type=cls.Types.FLAVOR, key=droplet.size_name)] = 1
        return consumables


CostTrackingRegister.register_strategy(DropletStrategy)
