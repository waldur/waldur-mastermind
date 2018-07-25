from __future__ import unicode_literals

from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers

from . import models, attribute_types


class ServiceProviderSerializer(core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.ServiceProvider
        fields = ('url', 'uuid', 'created', 'customer', 'customer_name', 'enable_notifications')
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation')
        }
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-service-provider-detail'},
            'customer': {'lookup_field': 'uuid'},
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['customer'])
        return attrs


class NesterAttributeOptionSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.AttributeOption
        fields = ('key', 'title')


class NestedAttributeSerializer(serializers.ModelSerializer):
    options = NesterAttributeOptionSerializer(many=True)

    class Meta(object):
        model = models.Attribute
        fields = ('key', 'title', 'type', 'options')


class NestedSectionSerializer(serializers.ModelSerializer):
    attributes = NestedAttributeSerializer(many=True, read_only=True)

    class Meta(object):
        model = models.Section
        fields = ('key', 'title', 'attributes')


class CategorySerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    offering_count = serializers.SerializerMethodField()
    sections = NestedSectionSerializer(many=True, read_only=True)

    @staticmethod
    def eager_load(queryset):
        return queryset.prefetch_related('sections', 'sections__attributes')

    def get_offering_count(self, category):
        try:
            return category.quotas.get(name='offering_count').usage
        except ObjectDoesNotExist:
            return 0

    class Meta(object):
        model = models.Category
        fields = ('url', 'uuid', 'title', 'description', 'icon', 'offering_count', 'sections')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }


class OfferingSerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    attributes = serializers.JSONField(required=False)
    category_title = serializers.ReadOnlyField(source='category.title')
    order_item_count = serializers.SerializerMethodField()

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'created', 'name', 'description', 'full_description', 'customer',
                  'category', 'category_title', 'rating', 'attributes', 'geolocations',
                  'is_active', 'native_name', 'native_description', 'thumbnail', 'order_item_count')
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
            'customer': {'lookup_field': 'uuid', 'view_name': 'customer-detail'},
            'category': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }

    def get_order_item_count(self, offering):
        try:
            return offering.quotas.get(name='order_item_count').usage
        except ObjectDoesNotExist:
            return 0

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['customer'])

        offering_attributes = attrs.get('attributes')
        if offering_attributes is not None:
            if not isinstance(offering_attributes, dict):
                raise rf_exceptions.ValidationError({
                    'attributes': 'Dictionary is expected.'
                })

            category = attrs.get('category', getattr(self.instance, 'category', None))
            self._validate_attributes(offering_attributes, category)

        return attrs

    def _validate_attributes(self, offering_attributes, category):
        offering_attribute_keys = offering_attributes.keys()
        category_attributes = list(models.Attribute.objects.filter(section__category=category,
                                                                   key__in=offering_attribute_keys))
        for key, value in offering_attributes.items():
            match_attributes = filter(lambda a: a.key == key, category_attributes)
            attribute = match_attributes[0] if match_attributes else None

            if attribute:
                klass = attribute_types.get_attribute_type(attribute.type)
                if klass:
                    try:
                        klass.validate(value, list(attribute.options.values_list('key', flat=True)))
                    except ValidationError as e:
                        err = rf_exceptions.ValidationError({'attributes': e.message})
                        raise err


class ScreenshotSerializer(core_serializers.AugmentedSerializerMixin,
                           serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.Screenshots
        fields = ('url', 'uuid', 'name', 'description', 'image', 'thumbnail', 'offering')
        protected_fields = ('offering', 'image')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'offering': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['offering'].customer)
        return attrs


class ItemSerializer(core_serializers.AugmentedSerializerMixin,
                     serializers.HyperlinkedModelSerializer):

    offering_name = serializers.ReadOnlyField(source='offering.name')

    class Meta(object):
        model = models.OrderItem
        fields = ('offering', 'offering_name', 'attributes', 'cost')
        read_only_fields = ('cost',)
        protected_fields = ('offering',)
        extra_kwargs = {
            'offering': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
        }

    def validate_offering(self, offering):
        if not offering.is_active:
            raise rf_exceptions.ValidationError(_('Offering is not available.'))
        return offering


class OrderSerializer(structure_serializers.PermissionFieldFilteringMixin,
                      core_serializers.AugmentedSerializerMixin,
                      serializers.HyperlinkedModelSerializer):

    state = serializers.ReadOnlyField(source='get_state_display')
    items = ItemSerializer(many=True)

    class Meta(object):
        model = models.Order
        fields = ('url', 'uuid',
                  'created', 'created_by', 'created_by_username', 'created_by_full_name',
                  'approved_by', 'approved_at', 'approved_by_username', 'approved_by_full_name',
                  'project', 'state', 'items', 'total_cost',)
        read_only_fields = ('created_by', 'approved_by', 'approved_at', 'state', 'total_cost')
        protected_fields = ('project', 'items')
        related_paths = {
            'created_by': ('username', 'full_name'),
            'approved_by': ('username', 'full_name'),
        }
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'created_by': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
            'approved_by': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
            'project': {'lookup_field': 'uuid', 'view_name': 'project-detail'},
        }

    @transaction.atomic
    def create(self, validated_data):
        user = self.context['request'].user
        validated_data['created_by'] = user
        items = validated_data.pop('items')
        order = super(OrderSerializer, self).create(validated_data)
        models.OrderItem.objects.bulk_create([
            models.OrderItem(
                order=order,
                offering=item['offering'],
                attributes=item.get('attributes', {}),
            )
            for item in items
        ])
        return order

    def get_filtered_field_names(self):
        return 'project',


def get_is_service_provider(serializer, scope):
    customer = structure_permissions._get_customer(scope)
    return models.ServiceProvider.objects.filter(customer=customer).exists()


def add_service_provider(sender, fields, **kwargs):
    fields['is_service_provider'] = serializers.SerializerMethodField()
    setattr(sender, 'get_is_service_provider', get_is_service_provider)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_service_provider,
)
