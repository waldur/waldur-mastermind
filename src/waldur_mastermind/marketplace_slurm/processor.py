from rest_framework import serializers

from waldur_mastermind.marketplace import processors
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import views as slurm_views


class CreateAllocationProcessor(processors.BaseCreateResourceProcessor):
    viewset = slurm_views.AllocationViewSet

    fields = (
        'name',
        'description',
    )

    def get_post_data(self):
        if not self.order_item.limits:
            raise serializers.ValidationError('Quota are not defined.')

        payload = super(CreateAllocationProcessor, self).get_post_data()

        for component_type in manager.get_component_types(PLUGIN_NAME):
            try:
                limit = self.order_item.limits[component_type]
            except KeyError:
                raise serializers.ValidationError('%s component quota is not defined' % component_type)
            else:
                payload[component_type + '_limit'] = limit
        return payload


class DeleteAllocationProcessor(processors.DeleteResourceProcessor):
    viewset = slurm_views.AllocationViewSet
