import logging

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.media.serializers import ProtectedImageField
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace.serializers import (
    MarketplaceProtectedMediaSerializerMixin,
)

from . import models

logger = logging.getLogger(__name__)


class CallManagingOrganisationSerializer(
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.CallManagingOrganisation
        fields = (
            'url',
            'uuid',
            'created',
            'description',
            'customer',
            'customer_name',
            'customer_uuid',
            'customer_image',
            'customer_abbreviation',
            'customer_native_name',
            'customer_country',
            'image',
        )
        related_paths = {'customer': ('uuid', 'name', 'native_name', 'abbreviation')}
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
            },
            'customer': {'lookup_field': 'uuid'},
        }

    customer_image = ProtectedImageField(source='customer.image', read_only=True)
    customer_country = serializers.CharField(source='customer.country', read_only=True)

    def get_fields(self):
        fields = super().get_fields()
        if settings.WALDUR_MARKETPLACE['ANONYMOUS_USER_CAN_VIEW_OFFERINGS']:
            fields['customer_image'] = serializers.ImageField(
                source='customer.image', read_only=True
            )
        return fields

    def validate(self, attrs):
        if not self.instance:
            marketplace_permissions.can_register_service_provider(
                self.context['request'], attrs['customer']
            )
        return attrs


class NestedRequestedOfferingSerializer(serializers.HyperlinkedModelSerializer):
    state = serializers.ReadOnlyField(source='get_state_display')
    offering_name = serializers.ReadOnlyField(source='offering.name')

    class Meta:
        model = models.RequestedOffering
        fields = [
            'uuid',
            'approved_by',
            'created_by',
            'state',
            'offering',
            'offering_name',
            'attributes',
        ]
        extra_kwargs = {
            'approved_by': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            },
            'created_by': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            },
            'offering': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-public-offering-detail',
            },
        }

    def get_url(self, requested_offering):
        return self.context['request'].build_absolute_uri(
            reverse(
                'proposal-call-offering-detail',
                kwargs={
                    'uuid': requested_offering.call.uuid.hex,
                    'requested_offering_uuid': requested_offering.uuid.hex,
                },
            )
        )


class PublicCallSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField(source='get_state_display')
    round_strategy = serializers.ReadOnlyField(source='get_round_strategy_display')
    allocation_strategy = serializers.ReadOnlyField(
        source='get_allocation_strategy_display'
    )
    review_strategy = serializers.ReadOnlyField(source='get_review_strategy_display')
    customer_name = serializers.ReadOnlyField(source='manager.customer.name')
    offerings = NestedRequestedOfferingSerializer(
        many=True, read_only=True, source='requestedoffering_set'
    )

    class Meta:
        model = models.Call
        fields = (
            'url',
            'uuid',
            'created',
            'name',
            'description',
            'start_time',
            'end_time',
            'description',
            'round_strategy',
            'review_strategy',
            'allocation_strategy',
            'state',
            'manager',
            'customer_name',
            'created_by',
            'offerings',
        )
        view_name = 'proposal-public-call-detail'
        read_only_fields = ('created_by',)
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
            },
            'manager': {
                'lookup_field': 'uuid',
                'view_name': 'call-managing-organisation-detail',
            },
            'created_by': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            },
        }


class RequestedOfferingSerializer(
    core_serializers.AugmentedSerializerMixin, NestedRequestedOfferingSerializer
):
    url = serializers.SerializerMethodField()

    class Meta(NestedRequestedOfferingSerializer.Meta):
        fields = NestedRequestedOfferingSerializer.Meta.fields + ['url']
        read_only_fields = (
            'created_by',
            'approved_by',
        )
        protected_fields = ('offering',)

    def get_url(self, requested_offering):
        return self.context['request'].build_absolute_uri(
            reverse(
                'proposal-call-offering-detail',
                kwargs={
                    'uuid': requested_offering.call.uuid.hex,
                    'requested_offering_uuid': requested_offering.uuid.hex,
                },
            )
        )

    def validate_offering(self, offering):
        user = self.context['request'].user

        if not (
            marketplace_models.Offering.objects.filter(id=offering.id)
            .filter_by_ordering_availability_for_user(user)
            .exists()
        ):
            raise serializers.ValidationError(
                {'offering': _('You do not have permissions for this offering.')}
            )

        return offering


class ProtectedCallSerializer(PublicCallSerializer):
    class Meta(PublicCallSerializer.Meta):
        view_name = 'proposal-protected-call-detail'
        protected_fields = ('manager',)

    def get_fields(self):
        fields = super().get_fields()
        try:
            method = self.context['view'].request.method
        except (KeyError, AttributeError):
            return fields

        if method in ('PUT', 'PATCH', 'POST'):
            fields['round_strategy'] = serializers.ChoiceField(
                models.Call.RoundStrategies.CHOICES, write_only=True
            )
            fields['review_strategy'] = serializers.ChoiceField(
                models.Call.ReviewStrategies.CHOICES, write_only=True
            )
            fields['allocation_strategy'] = serializers.ChoiceField(
                models.Call.AllocationStrategies.CHOICES, write_only=True
            )

        return fields

    def validate(self, attrs):
        start_time = attrs.get('start_time')
        end_time = attrs.get('end_time')

        if start_time and end_time and end_time <= start_time:
            raise serializers.ValidationError(
                {'start_time': _('Start time must be grow then end time.')}
            )

        manager = attrs.get('manager')
        user = self.context['request'].user

        if manager and not user.is_staff and user not in manager.customer.get_users():
            raise serializers.ValidationError(
                {
                    'manager': _(
                        'Current user has not permissions for selected organisation.'
                    )
                }
            )

        return attrs

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)
