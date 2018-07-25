""" Defines how to optimize AWS instances sizes """
import collections

from rest_framework import serializers as rf_serializers

from waldur_aws import (
    apps as aws_apps, models as aws_models, serializers as aws_serializers, cost_tracking as aws_cost_tracking)

from .. import optimizers, register, serializers
from . import utils


OptimizedPreset = collections.namedtuple('OptimizedPreset', ('preset', 'size', 'quantity', 'price'))

OptimizedAWS = optimizers.namedtuple_with_defaults(
    'OptimizedAWS',
    field_names=optimizers.OptimizedService._fields + ('optimized_presets',),
    default_values=optimizers.OptimizedService._defaults,
)


class AWSOptimizer(optimizers.Optimizer):
    """ Find the cheapest AWS size for each preset """
    HOURS_IN_DAY = 24
    DAYS_IN_MONTH = 30

    def _get_size_prices(self, sizes, service):
        """ Return dictionary with items <size>: <size price> """
        service_price_list_items = utils.get_service_price_list_items(service, aws_models.Instance)
        size_prices = {item.key: item.value for item in service_price_list_items
                       if item.item_type == aws_cost_tracking.InstanceStrategy.Types.FLAVOR}
        return {size: size_prices.get(size.backend_id, size.price) * self.HOURS_IN_DAY for size in sizes}

    def optimize(self, deployment_plan, service):
        optimized_presets = []
        price = 0
        sizes = aws_models.Size.objects.all()
        size_prices = self._get_size_prices(sizes, service)
        for item in deployment_plan.items.all():
            preset = item.preset
            sizes = [size for size in sizes
                     if size.cores >= preset.cores and size.ram >= preset.ram and size.disk >= preset.storage]
            if not sizes:
                preset_as_str = '%s (cores: %s, ram %s MB, storage %s MB)' % (
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
        return OptimizedAWS(price=price, service=service, optimized_presets=optimized_presets)


register.Register.register_optimizer(aws_apps.AWSConfig.service_name, AWSOptimizer)


class OptimizedPresetSerializer(rf_serializers.Serializer):
    size = aws_serializers.SizeSerializer()
    preset = serializers.PresetSerializer()
    quantity = rf_serializers.IntegerField()
    price = rf_serializers.DecimalField(max_digits=22, decimal_places=10)


class OptimizedAWSSerializer(serializers.OptimizedServiceSerializer):
    service = rf_serializers.HyperlinkedRelatedField(
        view_name='aws-detail',
        lookup_field='uuid',
        read_only=True,
    )
    optimized_presets = OptimizedPresetSerializer(many=True)


register.Register.register_serializer(aws_apps.AWSConfig.service_name, OptimizedAWSSerializer)
