from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers, status
from rest_framework.reverse import reverse

from waldur_mastermind.common.utils import internal_api_request
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import views as support_views


def process_support(order_item, user):
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
    if description:
        post_data['description'] = description
    if order_item.attributes:
        post_data['attributes'] = order_item.attributes
    post_data.update(order_item.attributes)

    view = support_views.OfferingViewSet.as_view({'post': 'create'})
    response = internal_api_request(view, user, post_data)
    if response.status_code != status.HTTP_201_CREATED:
        raise serializers.ValidationError(response.data)

    offering_uuid = response.data['uuid']
    offering = support_models.Offering.objects.get(uuid=offering_uuid)
    order_item.scope = offering
    order_item.save()
