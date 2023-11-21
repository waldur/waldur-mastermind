import logging

from django.conf import settings
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.media.serializers import ProtectedImageField
from waldur_mastermind.marketplace import permissions as marketplace_permissions
from waldur_mastermind.marketplace.serializers import (
    MarketplaceProtectedMediaSerializerMixin,
)

from . import models

logger = logging.getLogger(__name__)


class CallManagerSerializer(
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.CallManager
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
                'view_name': 'proposal-call-manager-detail',
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
