import logging

from django.contrib.contenttypes.models import ContentType
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
    connected_issue_count = issues.count()
    if connected_issue_count == 0:
        return
    if connected_issue_count > 1:
        logger.error(
            'Order item has %s instead of 1 issues connected. Unable to select. Order item UUID: %s',
            connected_issue_count,
            scope.uuid.hex,
        )

    issue = issues[0]

    issue_map = {'key': issue.key, 'uuid': issue.uuid.hex}
    return issue_map


def add_issue(sender, fields, **kwargs):
    fields['issue'] = serializers.SerializerMethodField()
    setattr(sender, 'get_issue', get_issue)


core_signals.pre_serializer_fields.connect(
    sender=marketplace_serializers.OrderItemDetailsSerializer, receiver=add_issue,
)
