import json

from django.core.exceptions import ObjectDoesNotExist
from rest_framework.reverse import reverse
from rest_framework import serializers, status
from rest_framework.test import APIRequestFactory

from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages import views as package_views
from waldur_openstack.openstack import models as openstack_models


factory = APIRequestFactory()


def process_order_item(order_item, request):
    try:
        template = order_item.offering.scope
    except ObjectDoesNotExist:
        template = None

    if not isinstance(template, package_models.PackageTemplate):
        raise serializers.ValidationError('Offering has invalid scope. VPC package template is expected.')

    project = order_item.order.project

    try:
        spl = openstack_models.OpenStackServiceProjectLink.objects.get(
            project=project,
            service__settings=template.service_settings,
            service__customer=project.customer,
        )
    except openstack_models.OpenStackServiceProjectLink.DoesNotExist:
        raise serializers.ValidationError('Project does not have access to the OpenStack service.')

    project_url = reverse('project-detail', kwargs={'uuid': project.uuid})
    spl_url = reverse('openstack-spl-detail', kwargs={'pk': spl.pk}, request=request)
    template_url = reverse('package-template-detail', kwargs={'uuid': template.uuid})

    post_data = dict(
        name=order_item.attributes['name'],
        description=order_item.attributes.get('description'),
        user_username=order_item.attributes.get('user_username'),
        service_project_link=spl_url,
        project=project_url,
        template=template_url,
    )
    request = factory.post('/', data=json.dumps(post_data), content_type='application/json',
                           HTTP_AUTHORIZATION='Token %s' % request.user.auth_token.key)

    view = package_views.OpenStackPackageViewSet.as_view({'post': 'create'})
    response = view(request)
    if response.status_code != status.HTTP_201_CREATED:
        raise serializers.ValidationError(response.data)

    package_uuid = response.data['uuid']
    package = package_models.OpenStackPackage.objects.get(uuid=package_uuid)
    order_item.scope = package
    order_item.save()
