from django.contrib.contenttypes.models import ContentType

from waldur_core.core import utils as core_utils
from waldur_mastermind.support import serializers as support_serializers


class OfferingCreateSerializer(support_serializers.OfferingCreateSerializer):
    def _get_issue_details(self, validated_data):
        issue_details = super(OfferingCreateSerializer, self)._get_issue_details(validated_data)
        order_item_serialized = self.context['request'].data.get('order_item')

        if order_item_serialized:
            order_item = core_utils.deserialize_instance(order_item_serialized)
            issue_details['resource_object_id'] = order_item.id
            issue_details['resource_content_type'] = ContentType.objects.get_for_model(order_item)
            issue_details['caller'] = order_item.order.created_by

        return issue_details
