from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import views as support_views


class CreateResourceProcessor(marketplace_utils.CreateResourceProcessor):
    def get_serializer_class(self):
        return support_views.OfferingViewSet.create_serializer_class

    def get_viewset(self):
        return support_views.OfferingViewSet

    def get_post_data(self):
        return get_post_data(self.order_item)

    def get_scope_from_response(self, response):
        return support_models.Offering.objects.get(uuid=response.data['uuid'])


class DeleteResourceProcessor(marketplace_utils.DeleteResourceProcessor):
    def get_viewset(self):
        return support_views.OfferingViewSet


def get_post_data(order_item):
    try:
        template = order_item.offering.scope
    except ObjectDoesNotExist:
        template = None

    if not isinstance(template, support_models.OfferingTemplate):
        raise serializers.ValidationError('Offering has invalid scope. Support template is expected.')

    project = order_item.order.project
    project_url = reverse('project-detail', kwargs={'uuid': project.uuid})
    template_url = reverse('support-offering-template-detail', kwargs={'uuid': template.uuid})

    post_data = dict(
        project=project_url,
        template=template_url,
        name=order_item.attributes.pop('name', ''),
    )

    description = order_item.attributes.pop('description', '')
    link_template = settings.WALDUR_MARKETPLACE['ORDER_ITEM_LINK_TEMPLATE']
    order_item_url = link_template.format(order_item_uuid=order_item.uuid,
                                          project_uuid=order_item.order.project.uuid)
    description += "\n[Order item|%s]." % order_item_url

    if order_item.limits:
        components_map = order_item.offering.get_usage_components()
        for key, value in order_item.limits.items():
            component = components_map[key]
            description += "\n%s (%s): %s %s" % \
                           (component.name, component.type, value, component.measured_unit)

    if description:
        post_data['description'] = description
    if order_item.attributes:
        post_data['attributes'] = order_item.attributes
    post_data.update(order_item.attributes)
    return post_data
