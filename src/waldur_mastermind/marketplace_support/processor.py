from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers, status
from rest_framework.reverse import reverse

from waldur_mastermind.common.utils import internal_api_request
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import views as support_views


def process_support(order_item, request):
    try:
        template = support_models.OfferingTemplate.objects.get(name=order_item.offering.name)
    except ObjectDoesNotExist:
        raise serializers.ValidationError('Offering does not have a template.')

    project = order_item.order.project
    project_url = reverse('project-detail', kwargs={'uuid': project.uuid})
    template_url = reverse('support-offering-template-detail', kwargs={'uuid': template.uuid})

    post_data = dict(
        name=order_item.attributes.get('name'),
        description=order_item.attributes.get('description'),
        project=project_url,
        template=template_url,
    )
    view = support_views.OfferingViewSet.as_view({'post': 'create'})
    response = internal_api_request(view, request.user, post_data)
    if response.status_code != status.HTTP_201_CREATED:
        raise serializers.ValidationError(response.data)

    offering_uuid = response.data['uuid']
    offering = support_models.Offering.objects.get(uuid=offering_uuid)
    order_item.scope = offering
    order_item.save()
