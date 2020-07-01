import logging

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import serializers

from waldur_core.core import signals as core_signals
from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import serializers as support_serializers

logger = logging.getLogger(__name__)


class OfferingCreateSerializer(support_serializers.OfferingCreateSerializer):
    def _get_issue_details(self, validated_data):
        issue_details = super(OfferingCreateSerializer, self)._get_issue_details(
            validated_data
        )
        order_item_serialized = self.context['request'].data.get('order_item')

        if order_item_serialized:
            order_item = core_utils.deserialize_instance(order_item_serialized)
            issue_details['resource_object_id'] = order_item.id
            issue_details['resource_content_type'] = ContentType.objects.get_for_model(
                order_item
            )
            issue_details['caller'] = order_item.order.created_by

        return issue_details


def get_issue(serializer, scope):
    issues = support_models.Issue.objects.filter(
        resource_object_id=scope.id,
        resource_content_type_id=ContentType.objects.get_for_model(scope).id,
    )
    urls = [
        {issue.key: reverse('support-issue-detail', args=[issue.uuid.hex])}
        for issue in issues
        if issue.key
    ]
    urls_count = len(urls)

    if urls_count == 1:
        return urls[0]

    elif urls_count > 1:
        logger.error(
            'Order item has %s instead of 1. Unable to select. Order item UUID: %s',
            urls_count,
            scope.uuid.hex,
        )
        return urls
    else:
        return


def add_issue(sender, fields, **kwargs):
    fields['issue'] = serializers.SerializerMethodField()
    setattr(sender, 'get_issue', get_issue)


core_signals.pre_serializer_fields.connect(
    sender=marketplace_serializers.OrderItemDetailsSerializer, receiver=add_issue,
)
