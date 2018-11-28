from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME
from waldur_slurm import models as slurm_models
from waldur_slurm import views as slurm_views
from waldur_slurm.apps import SlurmConfig


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
    try:
        service_settings = order_item.offering.scope
    except ObjectDoesNotExist:
        service_settings = None

    if not isinstance(service_settings, structure_models.ServiceSettings):
        raise serializers.ValidationError('Offering has invalid scope. Service settings is expected.')

    if service_settings.type != SlurmConfig.service_name:
        raise serializers.ValidationError('Offering has invalid scope type.')

    project = order_item.order.project

    try:
        spl = slurm_models.SlurmServiceProjectLink.objects.get(
            project=project,
            service__settings=service_settings,
            service__customer=project.customer,
        )
    except slurm_models.SlurmServiceProjectLink.DoesNotExist:
        raise serializers.ValidationError('Project does not have access to the SLURM service.')

    spl_url = reverse('slurm-spl-detail', kwargs={'pk': spl.pk})
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
