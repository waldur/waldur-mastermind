from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.structure.models import CUSTOMER_DETAILS_FIELDS
from waldur_core.structure.serializers import (
    CountrySerializerMixin,
    ProjectDetailsSerializerMixin,
)
from waldur_mastermind.marketplace.serializers import BaseItemSerializer

from . import models


class ReviewCommentSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False)


class ReviewSerializerMixin(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField(source='get_state_display')
    uuid = serializers.ReadOnlyField(source='flow.uuid')
    created = serializers.ReadOnlyField(source='flow.created')
    requested_by_full_name = serializers.ReadOnlyField(
        source='flow.requested_by.full_name'
    )
    reviewed_by_full_name = serializers.ReadOnlyField(source='reviewed_by.full_name')

    class Meta:
        model = models.ReviewMixin
        extra_kwargs = {
            'reviewed_by': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
        }
        fields = (
            'uuid',
            'reviewed_by',
            'reviewed_by_full_name',
            'requested_by_full_name',
            'reviewed_at',
            'review_comment',
            'state',
            'created',
        )


class CustomerCreateRequestSerializer(CountrySerializerMixin, ReviewSerializerMixin):
    class Meta(ReviewSerializerMixin.Meta):
        model = models.CustomerCreateRequest
        fields = ReviewSerializerMixin.Meta.fields + CUSTOMER_DETAILS_FIELDS


class ProjectCreateRequestSerializer(
    ProjectDetailsSerializerMixin, ReviewSerializerMixin
):
    class Meta(ReviewSerializerMixin.Meta):
        model = models.ProjectCreateRequest
        fields = ReviewSerializerMixin.Meta.fields + ('name', 'description', 'end_date')


class ResourceCreateRequestSerializer(BaseItemSerializer, ReviewSerializerMixin):
    uuid = serializers.ReadOnlyField(source='flow.uuid')

    class Meta(BaseItemSerializer.Meta):
        model = models.ResourceCreateRequest
        fields = (
            ReviewSerializerMixin.Meta.fields
            + BaseItemSerializer.Meta.fields
            + ('name', 'description', 'end_date')
        )
        extra_kwargs = {
            **BaseItemSerializer.Meta.extra_kwargs,
            'reviewed_by': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
        }


class FlowSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField(source='get_state_display')
    customer_create_request = CustomerCreateRequestSerializer(required=False)
    project_create_request = ProjectCreateRequestSerializer()
    resource_create_request = ResourceCreateRequestSerializer()

    def get_fields(self):
        fields = super().get_fields()
        try:
            request = self.context['view'].request
        except (KeyError, AttributeError):
            return fields

        if request.method in ('PUT', 'PATCH'):
            fields['resource_create_request'] = ResourceCreateRequestSerializer(
                instance=self.instance.resource_create_request
            )
        return fields

    class Meta:
        model = models.FlowTracker
        fields = (
            'uuid',
            'url',
            'customer',
            'order_item',
            'customer_create_request',
            'project_create_request',
            'resource_create_request',
            'state',
        )
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-resource-creation-flow-detail',
            },
            'customer': {'lookup_field': 'uuid', 'view_name': 'customer-detail'},
            'order_item': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-order-item-detail',
            },
        }
        read_only_fields = ('requested_by', 'order_item')

    def create(self, validated_data):
        request = self.context['request']
        customer = validated_data.get('customer')

        customer_create_request_data = validated_data.pop(
            'customer_create_request', None
        )
        project_create_request_data = validated_data.pop('project_create_request')
        resource_create_request_data = validated_data.pop('resource_create_request')

        if not customer_create_request_data and not customer:
            raise serializers.ValidationError(
                _('Either customer_create_request or customer should be specified.')
            )

        if customer_create_request_data and customer:
            raise serializers.ValidationError(
                _('customer_create_request and customer are mutually exclusive.')
            )

        if customer and request.user not in customer.get_users():
            raise serializers.ValidationError(
                _('User is not connected to this customer.')
            )

        if not customer:
            validated_data[
                'customer_create_request'
            ] = models.CustomerCreateRequest.objects.create(
                **customer_create_request_data
            )

        validated_data[
            'project_create_request'
        ] = models.ProjectCreateRequest.objects.create(**project_create_request_data)

        validated_data[
            'resource_create_request'
        ] = models.ResourceCreateRequest.objects.create(**resource_create_request_data)

        validated_data['requested_by'] = request.user
        return super(FlowSerializer, self).create(validated_data)

    def update(self, instance, validated_data):
        for field in (
            'customer_create_request',
            'project_create_request',
            'resource_create_request',
        ):
            data = validated_data.pop(field, None)
            section = getattr(instance, field)
            if data:
                for k, v in data.items():
                    setattr(section, k, v)
            section.save()
        return super().update(instance, validated_data)
