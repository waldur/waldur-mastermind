import copy

from waldur_mastermind.marketplace import serializers as marketplace_serializers


class BookingResourceSerializer(marketplace_serializers.ResourceSerializer):
    class Meta(marketplace_serializers.ResourceSerializer.Meta):
        view_name = 'booking-resource-detail'
        fields = marketplace_serializers.ResourceSerializer.Meta.fields + ('url',)
        extra_kwargs = copy.copy(marketplace_serializers.ResourceSerializer.Meta.extra_kwargs)
        extra_kwargs['url'] = {'lookup_field': 'uuid', 'read_only': True}
