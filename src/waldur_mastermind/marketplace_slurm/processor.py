from rest_framework import serializers

from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.utils import get_spl_url
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm import views as slurm_views


class CreateResourceProcessor(marketplace_utils.CreateResourceProcessor):
    def get_serializer_class(self):
        return slurm_views.AllocationViewSet.serializer_class

    def get_viewset(self):
        return slurm_views.AllocationViewSet

    def get_post_data(self):
        return get_post_data(self.order_item)

    def get_scope_from_response(self, response):
        return slurm_models.Allocation.objects.get(uuid=response.data['uuid'])


class DeleteResourceProcessor(marketplace_utils.DeleteResourceProcessor):
    def get_viewset(self):
        return slurm_views.AllocationViewSet


def get_post_data(order_item):
    spl_url = get_spl_url(slurm_models.SlurmServiceProjectLink, order_item)
    payload = dict(
        name=order_item.offering.name,
        service_project_link=spl_url,
    )

    if not order_item.limits:
        raise serializers.ValidationError('Quota are not defined.')

    for component_type in manager.get_component_types(PLUGIN_NAME):
        try:
            limit = order_item.limits[component_type]
        except KeyError:
            raise serializers.ValidationError('%s component quota is not defined' % component_type)
        else:
            payload[component_type + '_limit'] = limit
    return payload
