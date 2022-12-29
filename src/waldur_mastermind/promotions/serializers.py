import datetime

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.promotions import models


class CampaignSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    offerings = serializers.SlugRelatedField(
        slug_field='uuid',
        queryset=marketplace_models.Offering.objects.all(),
        many=True,
        required=True,
    )

    required_offerings = serializers.SlugRelatedField(
        slug_field='uuid',
        queryset=marketplace_models.Offering.objects.all(),
        many=True,
        required=False,
    )

    state = serializers.ReadOnlyField(source='get_state_display')

    def get_fields(self):
        fields = super(core_serializers.AugmentedSerializerMixin, self).get_fields()
        core_serializers.pre_serializer_fields.send(
            sender=self.__class__, fields=fields, serializer=self
        )

        protected_fields = self.Meta.protected_fields

        try:
            method = self.context['view'].request.method
        except (KeyError, AttributeError):
            return fields

        if method in ('PUT', 'PATCH'):
            if not self.instance or self.instance.state != models.Campaign.States.DRAFT:
                protected_fields += (
                    self.Meta.protected_fields_if_campaign_has_been_started
                )

            for field in protected_fields:
                fields[field].read_only = True

        return fields

    def validate(self, attrs):
        attrs = super().validate(attrs)
        start_date = attrs.get('start_date')
        end_date = attrs.get('end_date')
        stock = attrs.get('stock')
        auto_apply = attrs.get('auto_apply', True)

        user = self.context['request'].user
        offerings = attrs.get('offerings', [])
        required_offerings = attrs.get('required_offerings', [])
        service_provider = attrs.get('service_provider') or (
            self.instance.service_provider if self.instance else None
        )

        if (
            not user.is_staff
            and not user.is_support
            and user not in service_provider.customer.get_owners()
            and user not in service_provider.customer.get_service_managers()
        ):
            raise serializers.ValidationError(
                {
                    'service_provider': _(
                        'You do not have permissions to create campaign for selected service provider.'
                    )
                }
            )

        for offering in offerings:
            if service_provider.customer != offering.customer:
                raise serializers.ValidationError(
                    {
                        'offering': _(
                            'You do not have permissions to create campaign in selected offering.'
                        )
                    }
                )

        for required_offering in required_offerings:
            if service_provider != required_offering.customer.service_provider:
                raise serializers.ValidationError(
                    {
                        'required_offering': _(
                            'You do not have permissions to create campaign in selected offering.'
                        )
                    }
                )

        if start_date and start_date < datetime.date.today():
            raise serializers.ValidationError(
                {'start_date': _('Campaign start cannot be before the current date.')}
            )

        if start_date and end_date and start_date > end_date:
            raise serializers.ValidationError(
                {'end_date': _('Campaign end cannot be before the start date.')}
            )

        if auto_apply and stock:
            raise serializers.ValidationError(
                {'stock': _('Stock cannot be defined if auto_apply is true.')}
            )

        return attrs

    def validate_offerings(self, offerings):
        if not offerings:
            raise serializers.ValidationError(
                {'offering': _('An offering must be specified.')}
            )
        return offerings

    class Meta:
        model = models.Campaign
        fields = (
            'url',
            'start_date',
            'end_date',
            'coupon',
            'discount_type',
            'discount',
            'stock',
            'description',
            'months',
            'auto_apply',
            'state',
            'service_provider',
            'offerings',
            'required_offerings',
        )

        extra_kwargs = {
            'service_provider': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-service-provider-detail',
            },
            'url': {
                'lookup_field': 'uuid',
                'view_name': 'promotions-campaign-detail',
            },
        }

        protected_fields = ('service_provider',)

        protected_fields_if_campaign_has_been_started = (
            'start_date',
            'end_date',
            'discount_type',
            'discount',
            'stock',
            'months',
            'auto_apply',
            'offerings',
            'required_offerings',
        )

        read_only_fields = ('state',)

    def create(self, validated_data):
        offerings = validated_data.pop('offerings')
        required_offerings = validated_data.pop('required_offerings', [])
        campaign = super().create(validated_data)

        campaign.offerings.add(*offerings)
        campaign.required_offerings.add(*required_offerings)
        campaign.save()
        return campaign

    def update(self, instance, validated_data):
        offerings = validated_data.pop('offerings', [])
        required_offerings = validated_data.pop('required_offerings', [])
        campaign = super().update(instance, validated_data)

        if self.instance.state == models.Campaign.States.DRAFT:
            campaign.offerings.clear()
            campaign.offerings.add(*offerings)

            campaign.required_offerings.clear()
            campaign.required_offerings.add(*required_offerings)
            campaign.save()

        return campaign


def get_promotion_campaigns(serializer, offering):
    campaigns = []
    today = datetime.date.today()

    for campaign in models.Campaign.objects.filter(
        offerings=offering,
        start_date__lte=today,
        end_date__gte=today,
        state=models.Campaign.States.ACTIVE,
    ):
        try:

            class KlassSerializer(CampaignSerializer):
                def get_fields(self):
                    fields = super().get_fields()
                    fields.pop('url')
                    fields.pop('offerings')
                    fields.pop('required_offerings')
                    fields.pop('coupon')
                    fields.pop('state')
                    fields.pop('auto_apply')
                    return fields

            campaigns.append(
                KlassSerializer(instance=campaign, context=serializer.context).data
            )
        except IndexError:
            continue

    return campaigns


def add_promotion_campaigns(sender, fields, **kwargs):
    fields['promotion_campaigns'] = serializers.SerializerMethodField()
    setattr(sender, 'get_promotion_campaigns', get_promotion_campaigns)


core_signals.pre_serializer_fields.connect(
    sender=marketplace_serializers.PublicOfferingDetailsSerializer,
    receiver=add_promotion_campaigns,
)
