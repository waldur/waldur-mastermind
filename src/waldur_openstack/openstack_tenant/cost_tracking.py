from waldur_core.cost_tracking import CostTrackingStrategy, ConsumableItem, CostTrackingRegister

from . import models, utils, PriceItemTypes


class InstanceStrategy(CostTrackingStrategy):
    resource_class = models.Instance

    class Types(object):
        FLAVOR = PriceItemTypes.FLAVOR

    @classmethod
    def get_consumable_items(cls):
        for flavor_name in set(models.Flavor.objects.all().values_list('name', flat=True)):
            yield utils.get_consumable_item(flavor_name)

    @classmethod
    def get_configuration(cls, instance):
        consumables = {}
        if instance.state != models.Instance.States.ERRED:
            consumables[ConsumableItem(item_type=cls.Types.FLAVOR, key=instance.flavor_name)] = 1
        return consumables


CostTrackingRegister.register_strategy(InstanceStrategy)


class VolumeStrategy(CostTrackingStrategy):
    resource_class = models.Volume

    class Types(object):
        STORAGE = PriceItemTypes.STORAGE

    class Keys(object):
        STORAGE = '1 GB'

    @classmethod
    def get_consumable_items(cls):
        return [ConsumableItem(item_type=cls.Types.STORAGE, key=cls.Keys.STORAGE, name='1 GB of storage', units='GB')]

    @classmethod
    def get_configuration(cls, volume):
        return {ConsumableItem(item_type=cls.Types.STORAGE, key=cls.Keys.STORAGE): float(volume.size) / 1024}


CostTrackingRegister.register_strategy(VolumeStrategy)


class SnapshotStrategy(CostTrackingStrategy):
    resource_class = models.Snapshot

    class Types(object):
        STORAGE = PriceItemTypes.STORAGE

    class Keys(object):
        STORAGE = '1 GB'

    @classmethod
    def get_consumable_items(cls):
        return [ConsumableItem(item_type=cls.Types.STORAGE, key=cls.Keys.STORAGE, name='1 GB of storage', units='GB')]

    @classmethod
    def get_configuration(cls, snapshot):
        return {ConsumableItem(item_type=cls.Types.STORAGE, key=cls.Keys.STORAGE): float(snapshot.size) / 1024}


CostTrackingRegister.register_strategy(SnapshotStrategy)
