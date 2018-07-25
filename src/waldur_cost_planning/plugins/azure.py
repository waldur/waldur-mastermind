""" Azure instances optimization """
import collections

from rest_framework import serializers as rf_serializers

from waldur_azure import (apps as azure_apps, models as azure_models, serializers as azure_serializers,
                          cost_tracking as azure_cost_tracking, backend as azure_backend)

from .. import optimizers, register, serializers
from . import utils


OptimizedPreset = collections.namedtuple('OptimizedPreset', ('preset', 'size', 'quantity', 'price'))

OptimizedAzure = optimizers.namedtuple_with_defaults(
    'OptimizedAzure',
    field_names=optimizers.OptimizedService._fields + ('optimized_presets',),
    default_values=optimizers.OptimizedService._defaults,
)


class AzureOptimizer(optimizers.Optimizer):
    """ Find the cheapest Azure size for each preset """
    HOURS_IN_DAY = 24
    DAYS_IN_MONTH = 30

    def _get_size_prices(self, sizes, service):
        """ Return dictionary with items <size>: <size price> """
        service_price_list_items = utils.get_service_price_list_items(service, azure_models.VirtualMachine)
        size_prices = {item.key: item.value for item in service_price_list_items
                       if item.item_type == azure_cost_tracking.AzureCostTrackingStrategy.Types.FLAVOR}
        if not size_prices:
            raise optimizers.OptimizationError('Size prices are missing.')

        return {size: size_prices.get(size[2], size.price) * self.HOURS_IN_DAY for size in sizes}

    def optimize(self, deployment_plan, service):
        optimized_presets = []
        price = 0
        sizes = azure_backend.SizeQueryset().all()
        size_prices = self._get_size_prices(sizes, service)
        for item in deployment_plan.items.all():
            preset = item.preset
            sizes = [size for size in sizes
                     if size.cores >= preset.cores and size.ram >= preset.ram and size.disk >= preset.storage]
            if not sizes:
                preset_as_str = '%s (cores: %s, ram: %s MB, storage: %s MB)' % (
                    preset.name, preset.cores, preset.ram, preset.storage)
                raise optimizers.OptimizationError(
                    'It is impossible to create an instance for preset %s. It is too big.' % preset_as_str)
            optimal_size = min(sizes, key=lambda size: size_prices[size])
            optimized_presets.append(OptimizedPreset(
                preset=preset,
                size=optimal_size,
                quantity=item.quantity,
                price=size_prices[optimal_size] * item.quantity,
            ))
            price += size_prices[optimal_size] * item.quantity
        return OptimizedAzure(price=price, service=service, optimized_presets=optimized_presets)


register.Register.register_optimizer(azure_apps.AzureConfig.service_name, AzureOptimizer)


class OptimizedPresetSerializer(rf_serializers.Serializer):
    size = azure_serializers.SizeSerializer()
    preset = serializers.PresetSerializer()
    quantity = rf_serializers.IntegerField()
    price = rf_serializers.DecimalField(max_digits=22, decimal_places=10)


class OptimizedAzureSerializer(serializers.OptimizedServiceSerializer):
    service = rf_serializers.HyperlinkedRelatedField(
        view_name='azure-detail',
        lookup_field='uuid',
        read_only=True,
    )
    optimized_presets = OptimizedPresetSerializer(many=True)


register.Register.register_serializer(azure_apps.AzureConfig.service_name, OptimizedAzureSerializer)
