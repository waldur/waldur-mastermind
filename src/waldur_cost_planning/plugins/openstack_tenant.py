""" Defines how to optimize price for OpenStackTenant instances """
import collections

from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers as rf_serializers

from waldur_core.cost_tracking import models as cost_tracking_models
from waldur_openstack.openstack_tenant import (
    apps as ot_apps, models as ot_models, serializers as ot_serializers, cost_tracking as ot_cost_tracking)

from .. import optimizers, register, serializers


OptimizedPreset = collections.namedtuple(
    'OptimizedPreset', ('preset', 'flavor', 'quantity', 'price', 'flavor_price', 'storage_price'))


OptimizedOpenStackTenant = optimizers.namedtuple_with_defaults(
    'OptimizedOpenStack',
    field_names=optimizers.OptimizedService._fields + ('optimized_presets',),
    default_values=optimizers.OptimizedService._defaults,
)


class OpenStackTenantOptimizer(optimizers.Optimizer):
    """ Find the cheapest OpenStackTenant flavor for each preset. """
    HOURS_IN_DAY = 24

    def _get_service_price_item(self, service, resource_content_type, item_type, key):
        default_item = cost_tracking_models.DefaultPriceListItem.objects.get(
            resource_content_type=resource_content_type, item_type=item_type, key=key)
        try:
            return cost_tracking_models.PriceListItem.objects.get(
                default_price_list_item=default_item, service=service)
        except cost_tracking_models.PriceListItem.DoesNotExist:
            return default_item

    def _get_flavor_price(self, service, flavor):
        instance_content_type = ContentType.objects.get_for_model(ot_models.Instance)
        try:
            item = self._get_service_price_item(
                service=service,
                resource_content_type=instance_content_type,
                item_type=ot_cost_tracking.InstanceStrategy.Types.FLAVOR,
                key=flavor.name,
            )
        except cost_tracking_models.DefaultPriceListItem.DoesNotExist:
            raise optimizers.OptimizationError('Price is not defined for flavor "%s".' % flavor.name)
        return item.value * self.HOURS_IN_DAY

    def _get_cheapest_flavor(self, service, suitable_flavors):
        priced_flavors = [(flavor, self._get_flavor_price(service, flavor)) for flavor in suitable_flavors]
        cheapest_flavor, price = min(priced_flavors, key=lambda x: x[1])
        return cheapest_flavor, price

    def _get_storage_price(self, service, storage):
        volume_content_type = ContentType.objects.get_for_model(ot_models.Volume)
        try:
            item = self._get_service_price_item(
                service=service,
                resource_content_type=volume_content_type,
                item_type=ot_cost_tracking.VolumeStrategy.Types.STORAGE,
                key=ot_cost_tracking.VolumeStrategy.Keys.STORAGE,
            )
        except cost_tracking_models.DefaultPriceListItem.DoesNotExist:
            raise optimizers.OptimizationError('Storage price is not defined.')
        return item.value * self.HOURS_IN_DAY

    def optimize(self, deployment_plan, service):
        optimized_presets = []
        price = 0
        for item in deployment_plan.items.all():
            preset = item.preset
            suitable_flavors = ot_models.Flavor.objects.filter(
                cores__gte=preset.cores, ram__gte=preset.ram, settings=service.settings)
            if not suitable_flavors:
                preset_as_str = '%s (cores: %s, ram %s MB, storage %s MB)' % (
                    preset.name, preset.cores, preset.ram, preset.storage)
                raise optimizers.OptimizationError(
                    'It is impossible to create an instance for preset %s. It is too big.' % preset_as_str)

            flavor, flavor_price = self._get_cheapest_flavor(service, suitable_flavors)
            storage_price = self._get_storage_price(service, preset.storage)
            preset_price = flavor_price + storage_price
            optimized_presets.append(OptimizedPreset(
                preset=preset,
                flavor=flavor,
                quantity=item.quantity,
                flavor_price=flavor_price,
                storage_price=storage_price,
                price=preset_price * item.quantity,
            ))
            price += preset_price * item.quantity
        return OptimizedOpenStackTenant(price=price, service=service, optimized_presets=optimized_presets)


register.Register.register_optimizer(ot_apps.OpenStackTenantConfig.service_name, OpenStackTenantOptimizer)


class OptimizedPresetSerializer(rf_serializers.Serializer):
    flavor = ot_serializers.FlavorSerializer()
    preset = serializers.PresetSerializer()
    quantity = rf_serializers.IntegerField()
    flavor_price = rf_serializers.DecimalField(max_digits=22, decimal_places=10)
    storage_price = rf_serializers.DecimalField(max_digits=22, decimal_places=10)
    price = rf_serializers.DecimalField(max_digits=22, decimal_places=10)


class OptimizedOpenStackTenantSerializer(serializers.OptimizedServiceSerializer):
    service = rf_serializers.HyperlinkedRelatedField(
        view_name='openstacktenant-detail',
        lookup_field='uuid',
        read_only=True,
    )
    optimized_presets = OptimizedPresetSerializer(many=True)


register.Register.register_serializer(ot_apps.OpenStackTenantConfig.service_name, OptimizedOpenStackTenantSerializer)
