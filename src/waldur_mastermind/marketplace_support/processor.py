from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.support import backend as support_backend
from waldur_mastermind.support import exceptions as support_exceptions
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import views as support_views
from waldur_mastermind.support import serializers as support_serializers


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

    def process_order_item(self, user):
        order_item_content_type = ContentType.objects.get_for_model(self.order_item)

        if not support_models.Issue.objects.filter(resource_object_id=self.order_item.id,
                                                   resource_content_type=order_item_content_type).exists():
            self.order_item.resource.set_state_terminating()
            self.order_item.resource.save(update_fields=['state'])

            link_template = settings.WALDUR_MARKETPLACE_SUPPORT['REQUEST_LINK_TEMPLATE']
            request_url = link_template.format(request_uuid=self.order_item.resource.scope.uuid)
            description = "\n[Terminate resource %s|%s]." % (self.order_item.resource.scope.name, request_url)
            issue_details = dict(
                caller=self.order_item.order.created_by,
                project=self.order_item.order.project,
                customer=self.order_item.order.project.customer,
                type=settings.WALDUR_SUPPORT['DEFAULT_OFFERING_ISSUE_TYPE'],
                description=description,
                summary='Request to terminate resource %s' % self.order_item.resource.scope.name,
                resource=self.order_item)
            issue_details['summary'] = support_serializers.render_issue_template('summary', issue_details)
            issue_details['description'] = support_serializers.render_issue_template('description', issue_details)
            issue = support_models.Issue.objects.create(**issue_details)
            try:
                support_backend.get_active_backend().create_issue(issue)
            except support_exceptions.SupportUserInactive:
                issue.delete()
                raise rf_exceptions.ValidationError(_('Delete resource process is cancelled and issue not created '
                                                      'because a caller is inactive.'))


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
