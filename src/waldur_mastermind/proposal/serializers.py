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
                    'obj_uuid': requested_offering.uuid.hex,
                },
            )
        )


class NestedRoundSerializer(serializers.HyperlinkedModelSerializer):
    review_strategy = serializers.ReadOnlyField(source='get_review_strategy_display')

    class Meta:
        model = models.Round
        fields = [
            'uuid',
            'start_time',
            'end_time',
            'review_strategy',
            'review_duration_in_days',
            'minimum_number_of_reviewers',
        ]


class PublicCallSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField(source='get_state_display')
    allocation_strategy = serializers.ReadOnlyField(
        source='get_allocation_strategy_display'
    )
    customer_name = serializers.ReadOnlyField(source='manager.customer.name')
    offerings = NestedRequestedOfferingSerializer(
        many=True, read_only=True, source='requestedoffering_set'
    )
    rounds = NestedRoundSerializer(many=True, read_only=True, source='round_set')

    class Meta:
        model = models.Call
        fields = (
            'url',
            'uuid',
            'created',
            'name',
            'description',
            'description',
            'allocation_strategy',
            'state',
            'manager',
            'customer_name',
            'offerings',
            'rounds',
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
                    'obj_uuid': requested_offering.uuid.hex,
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

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class ReviewerSerializer(serializers.HyperlinkedModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = models.CallReviewer
        fields = (
            'url',
            'uuid',
            'user',
            'created',
            'created_by',
        )
        read_only_fields = ('created_by',)
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
            },
            'user': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            },
            'created_by': {
                'lookup_field': 'uuid',
                'view_name': 'user-detail',
            },
        }

    def get_url(self, reviewer):
        return self.context['request'].build_absolute_uri(
            reverse(
                'proposal-call-reviewer-detail',
                kwargs={
                    'uuid': reviewer.call.uuid.hex,
                    'obj_uuid': reviewer.uuid.hex,
                },
            )
        )

    def validate_unique_together(self, call):
        user = self.validated_data['user']

        if call.callreviewer_set.filter(user_id=user.id).exists():
            raise serializers.ValidationError(
                {'user': _('User must by unique for a call.')}
            )

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class ProtectedCallSerializer(PublicCallSerializer):
    reviewers = ReviewerSerializer(many=True, read_only=True, source='callreviewer_set')

    class Meta(PublicCallSerializer.Meta):
        fields = PublicCallSerializer.Meta.fields + (
            'reviewers',
            'created_by',
        )
        view_name = 'proposal-protected-call-detail'
        protected_fields = ('manager',)

    def get_fields(self):
        fields = super().get_fields()
        try:
            method = self.context['view'].request.method
        except (KeyError, AttributeError):
            return fields

        if method in ('PUT', 'PATCH', 'POST'):
            fields['allocation_strategy'] = serializers.ChoiceField(
                models.Call.AllocationStrategies.CHOICES, write_only=True
            )

        return fields

    def validate(self, attrs):
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


class RoundSerializer(core_serializers.AugmentedSerializerMixin, NestedRoundSerializer):
    url = serializers.SerializerMethodField()

    class Meta(NestedRoundSerializer.Meta):
        fields = NestedRoundSerializer.Meta.fields + ['url']

    def get_url(self, call_round):
        return self.context['request'].build_absolute_uri(
            reverse(
                'proposal-call-round-detail',
                kwargs={
                    'uuid': call_round.call.uuid.hex,
                    'obj_uuid': call_round.uuid.hex,
                },
            )
        )

    def validate(self, attrs):
        start_time = attrs.get('start_time')
        end_time = attrs.get('end_time')

        if start_time and end_time and end_time <= start_time:
            raise serializers.ValidationError(
                {'start_time': _('Start time must be grow then end time.')}
            )

        return attrs


class ProposalSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField(source='get_state_display')
    round = NestedRoundSerializer(read_only=True)
    round_uuid = serializers.UUIDField(write_only=True, required=True)

    class Meta:
        model = models.Proposal
        fields = [
            'uuid',
            'url',
            'name',
            'state',
            'approved_by',
            'created_by',
            'duration_in_days',
            'project',
            'round',
            'round_uuid',
        ]
        read_only_fields = ('created_by', 'approved_by', 'project')
        protected_fields = ('round_uuid',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'created_by': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
            'approved_by': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
            'project': {'lookup_field': 'uuid', 'view_name': 'project-detail'},
        }

    def validate(self, attrs):
        if self.instance:
            return attrs

        round_uuid = attrs.pop('round_uuid')

        try:
            call_round = models.Round.objects.get(uuid=round_uuid)
        except models.Round.DoesNotExist:
            raise serializers.ValidationError({'round_uuid': _('Round not found.')})

        if call_round.call.state != models.Call.States.ACTIVE:
            raise serializers.ValidationError(_('Call is not active.'))

        attrs['round'] = call_round
        return attrs

    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class ReviewSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.ReadOnlyField(source='get_state_display')
    reviewer = serializers.SerializerMethodField()

    class Meta:
        model = models.Review
        fields = (
            'url',
            'uuid',
            'proposal',
            'reviewer',
            'state',
            'summary_score',
            'summary_public_comment',
            'summary_private_comment',
        )
        read_only_fields = ('proposal',)
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
            },
            'proposal': {
                'lookup_field': 'uuid',
                'view_name': 'proposal-proposal-detail',
            },
        }

    def get_reviewer(self, review):
        return self.context['request'].build_absolute_uri(
            reverse(
                'proposal-call-reviewer-detail',
                kwargs={
                    'uuid': review.proposal.round.call.uuid.hex,
                    'obj_uuid': review.uuid.hex,
                },
            )
        )

    def get_fields(self):
        fields = super().get_fields()

        if not self.instance:
            return fields
        elif isinstance(self.instance, list):
            review = self.instance[0]
        else:
            review = self.instance

        try:
            request = self.context['view'].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if (
            user.is_staff
            or review.reviewer.user == user
            or user in review.proposal.round.call.manager.customer.get_users()
        ):
            return fields

        del fields['summary_private_comment']
        del fields['reviewer']

        return fields
