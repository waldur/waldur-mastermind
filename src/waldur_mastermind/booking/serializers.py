import copy

from django.utils.lru_cache import lru_cache
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers


class BookingResourceSerializer(marketplace_serializers.ResourceSerializer):
    created_by = serializers.SerializerMethodField()
    created_by_username = serializers.SerializerMethodField()
    created_by_full_name = serializers.SerializerMethodField()
    approved_by = serializers.SerializerMethodField()
    approved_by_username = serializers.SerializerMethodField()
    approved_by_full_name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()

    class Meta(marketplace_serializers.ResourceSerializer.Meta):
        view_name = 'booking-resource-detail'
        fields = marketplace_serializers.ResourceSerializer.Meta.fields + (
            'url',
            'created_by',
            'created_by_username',
            'created_by_full_name',
            'approved_by',
            'approved_by_username',
            'approved_by_full_name',
            'description',
        )
        extra_kwargs = copy.copy(
            marketplace_serializers.ResourceSerializer.Meta.extra_kwargs
        )
        extra_kwargs['url'] = {'lookup_field': 'uuid', 'read_only': True}

    @lru_cache(maxsize=1)
    def _get_order_item(self, resource):
        return marketplace_models.OrderItem.objects.get(
            resource=resource, type__in=[marketplace_models.OrderItem.Types.CREATE],
        )

    def get_created_by(self, resource):
        order_item = self._get_order_item(resource)
        uuid = order_item.order.created_by.uuid.hex
        return reverse('user-detail', kwargs={'uuid': uuid},)

    def get_created_by_username(self, resource):
        order_item = self._get_order_item(resource)
        return order_item.order.created_by.username

    def get_created_by_full_name(self, resource):
        order_item = self._get_order_item(resource)
        return order_item.order.created_by.full_name

    def get_approved_by(self, resource):
        order_item = self._get_order_item(resource)
        if order_item.order.approved_by:
            uuid = order_item.order.approved_by.uuid.hex
            return reverse('user-detail', kwargs={'uuid': uuid},)

    def get_approved_by_username(self, resource):
        order_item = self._get_order_item(resource)
        if order_item.order.approved_by:
            return order_item.order.approved_by.username

    def get_approved_by_full_name(self, resource):
        order_item = self._get_order_item(resource)
        if order_item.order.approved_by:
            return order_item.order.approved_by.full_name

    def get_description(self, resource):
        return resource.attributes.get('description', '')


class BookingSerializer(serializers.Serializer):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
