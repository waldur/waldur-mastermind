from waldur_core.cost_tracking import CostTrackingRegister, CostTrackingStrategy, ConsumableItem

from . import models


class InstanceStrategy(CostTrackingStrategy):
    resource_class = models.Instance

    class Types(object):
        FLAVOR = 'flavor'

    @classmethod
    def get_consumable_items(cls):
        return [ConsumableItem(item_type=cls.Types.FLAVOR, key=size.backend_id, default_price=size.price)
                for size in models.Size.objects.all()]

    @classmethod
    def get_configuration(cls, instance):
        consumables = {}
        if instance.state != models.Instance.States.ERRED:
            consumables[ConsumableItem(item_type=cls.Types.FLAVOR, key=instance.size_backend_id)] = 1
        return consumables


CostTrackingRegister.register_strategy(InstanceStrategy)
