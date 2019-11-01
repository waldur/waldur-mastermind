from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import processors
from waldur_mastermind.marketplace.utils import get_order_item_url
from waldur_mastermind.marketplace_support.utils import format_description
from waldur_mastermind.support import models as support_models

from .views import IssueViewSet, OfferingViewSet


class CreateRequestProcessor(processors.BaseCreateResourceProcessor):
    viewset = OfferingViewSet

    def get_post_data(self):
        order_item = self.order_item
        try:
            template = order_item.offering.scope
        except ObjectDoesNotExist:
            template = None

        if not isinstance(template, support_models.OfferingTemplate):
            raise serializers.ValidationError('Offering has invalid scope. Support template is expected.')

        project = order_item.order.project
        project_url = reverse('project-detail', kwargs={'uuid': project.uuid.hex})
        template_url = reverse('support-offering-template-detail', kwargs={'uuid': template.uuid.hex})
        attributes = order_item.attributes.copy()

        post_data = dict(
            project=project_url,
            template=template_url,
            name=attributes.pop('name', ''),
            order_item=core_utils.serialize_instance(order_item),
        )

        description = attributes.pop('description', '')
        description += format_description('CREATE_RESOURCE_TEMPLATE', {
            'order_item': order_item,
            'order_item_url': get_order_item_url(order_item),
        })

        if order_item.limits:
            components_map = order_item.offering.get_usage_components()
            for key, value in order_item.limits.items():
                component = components_map.get(key)
                if component:
                    description += "\n%s (%s): %s %s" % \
                                   (component.name, component.type, value, component.measured_unit)

        if order_item.plan and order_item.plan.scope:
            post_data['plan'] = reverse('support-offering-plan-detail', kwargs={
                'uuid': order_item.plan.scope.uuid
            })

        if description:
            post_data['description'] = description
        if attributes:
            post_data['attributes'] = attributes
        return post_data


class DeleteRequestProcessor(processors.DeleteResourceProcessor):
    def get_viewset(self):
        return IssueViewSet

    def get_resource(self):
        return self.order_item


class UpdateRequestProcessor(processors.UpdateResourceProcessor):
    def get_view(self):
        return IssueViewSet.as_view({'post': 'update'})

    def get_post_data(self):
        return {'uuid': str(self.order_item.uuid)}
