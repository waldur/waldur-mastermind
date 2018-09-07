from __future__ import unicode_literals

from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers

from waldur_core.core import signals as core_signals
from waldur_core.core import serializers as core_serializers
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure import models as structure_models
from waldur_mastermind.common.serializers import validate_options

from . import models, attribute_types, plugins


class ServiceProviderSerializer(core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.ServiceProvider
        fields = ('url', 'uuid', 'created', 'customer', 'customer_name', 'customer_uuid', 'description', 'enable_notifications')
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


class NestedAttributeOptionSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.AttributeOption
        fields = ('key', 'title')


class NestedAttributeSerializer(serializers.ModelSerializer):
    options = NestedAttributeOptionSerializer(many=True)

    class Meta(object):
        model = models.Attribute
        fields = ('key', 'title', 'type', 'options', 'required',)


class NestedSectionSerializer(serializers.ModelSerializer):
    attributes = NestedAttributeSerializer(many=True, read_only=True)

    class Meta(object):
        model = models.Section
        fields = ('key', 'title', 'attributes', 'is_standalone')


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


class PlanComponentSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.PlanComponent
        fields = ('type', 'amount', 'price')


class PlanSerializer(core_serializers.AugmentedSerializerMixin,
                     serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.Plan
        fields = ('url', 'uuid', 'name', 'description', 'unit_price', 'unit', 'offering')
        protected_fields = ('offering',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-plan-detail'},
            'offering': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['offering'].customer)

        self._validate_components(attrs)
        return attrs

    def _validate_components(self, attrs):
        offering = attrs.get('offering', getattr(self.instance, 'offering', None))
        components = plugins.manager.get_components(offering.type)
        if components:
            expected = sorted(components.keys())
            actual = sorted(component['type'] for component in attrs.get('components', []))
            if actual != expected:
                raise serializers.ValidationError({'components': _('Invalid component types.')})
            attrs['unit_price'] = sum(component['amount'] * component['price']
                                      for component in attrs.get('components', []))


class NestedPlanSerializer(core_serializers.AugmentedSerializerMixin,
                           serializers.HyperlinkedModelSerializer):
    components = PlanComponentSerializer(many=True, required=False)

    class Meta(object):
        model = models.Plan
        fields = ('url', 'uuid', 'name', 'description', 'unit_price', 'unit', 'components')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-plan-detail'},
            'unit': {'required': True}
        }


class NestedScreenshotSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.Screenshot
        fields = ('name', 'description', 'image', 'thumbnail')


class ScreenshotSerializer(core_serializers.AugmentedSerializerMixin,
                           serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.Screenshot
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


FIELD_TYPES = (
    'boolean',
    'integer',
    'money',
    'string',
    'text',
    'html_text',
    'select_string',
    'select_openstack_tenant',
)


class DefaultField(serializers.Field):
    def to_internal_value(self, data):
        return data


class OptionFieldSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=FIELD_TYPES)
    label = serializers.CharField()
    help_text = serializers.CharField(required=False)
    required = serializers.BooleanField(default=False)
    choices = serializers.ListField(child=serializers.CharField(), required=False)
    default = DefaultField(required=False)
    min = serializers.IntegerField(required=False)
    max = serializers.IntegerField(required=False)


class OfferingOptionsSerializer(serializers.Serializer):
    order = serializers.ListField(child=serializers.CharField())
    options = serializers.DictField(child=OptionFieldSerializer())


class OfferingSerializer(core_serializers.AugmentedSerializerMixin,
                         core_serializers.RestrictedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    attributes = serializers.JSONField(required=False)
    options = serializers.JSONField(required=False)
    geolocations = core_serializers.GeoLocationField(required=False)
    order_item_count = serializers.SerializerMethodField()
    plans = NestedPlanSerializer(many=True, required=False)
    screenshots = NestedScreenshotSerializer(many=True, read_only=True)
    state = serializers.ReadOnlyField(source='get_state_display')

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'created', 'name', 'description', 'full_description',
                  'customer', 'customer_uuid', 'customer_name',
                  'category', 'category_uuid', 'category_title',
                  'rating', 'attributes', 'options', 'geolocations',
                  'state', 'native_name', 'native_description', 'vendor_details',
                  'thumbnail', 'order_item_count', 'plans', 'screenshots', 'type', 'shared')
        related_paths = {
            'customer': ('uuid', 'name'),
            'category': ('uuid', 'title'),
        }
        protected_fields = ('customer', 'type')
        read_only_fields = ('state',)
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

        self._validate_plans(attrs)
        return attrs

    def validate_type(self, offering_type):
        if offering_type not in plugins.manager.backends.keys():
            raise rf_exceptions.ValidationError(_('Invalid value.'))
        return offering_type

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

    def validate_options(self, options):
        serializer = OfferingOptionsSerializer(data=options)
        serializer.is_valid(raise_exception=True)
        return options

    def _validate_plans(self, attrs):
        offering_type = attrs.get('type', getattr(self.instance, 'type', None))
        components = plugins.manager.get_components(offering_type)
        if components:
            expected = sorted(components.keys())
            plans = attrs.get('plans', [])
            for plan in plans:
                actual = sorted(component['type'] for component in plan.get('components', []))
                if actual != expected:
                    raise serializers.ValidationError({'plans': _('Invalid plan components.')})
                plan['unit_price'] = sum(component['amount'] * component['price']
                                         for component in plan.get('components', []))

    @transaction.atomic
    def create(self, validated_data):
        plans = validated_data.pop('plans', [])
        offering = super(OfferingSerializer, self).create(validated_data)
        for plan_data in plans:
            components = plan_data.pop('components', [])
            plan = models.Plan.objects.create(offering=offering, **plan_data)
            for component_data in components:
                models.PlanComponent.objects.create(plan=plan, **component_data)
        return offering

    def update(self, instance, validated_data):
        # TODO: Implement support for nested plan update
        validated_data.pop('plans', [])
        offering = super(OfferingSerializer, self).update(instance, validated_data)
        return offering


class OrderItemSerializer(core_serializers.AugmentedSerializerMixin,
                          serializers.HyperlinkedModelSerializer):

    provider_name = serializers.ReadOnlyField(source='offering.customer.name')
    provider_uuid = serializers.ReadOnlyField(source='offering.customer.uuid')
    offering_thumbnail = serializers.FileField(source='offering.thumbnail', read_only=True)
    resource_uuid = serializers.SerializerMethodField()
    resource_type = serializers.SerializerMethodField()
    state = serializers.ReadOnlyField(source='get_state_display')

    def get_resource_uuid(self, order_item):
        if order_item.scope:
            return order_item.scope.uuid

    def get_resource_type(self, order_item):
        if order_item.scope:
            return order_item.scope.get_scope_type()

    class Meta(object):
        model = models.OrderItem
        fields = ('offering', 'offering_name', 'offering_uuid',
                  'offering_description', 'offering_thumbnail',
                  'provider_name', 'provider_uuid',
                  'attributes', 'cost', 'plan', 'resource_uuid', 'resource_type', 'state',)

        related_paths = {
            'offering': ('name', 'uuid', 'description'),
        }
        read_only_fields = ('cost', 'state',)
        protected_fields = ('offering', 'plan')
        extra_kwargs = {
            'offering': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
            'plan': {'lookup_field': 'uuid', 'view_name': 'marketplace-plan-detail'},
        }

    def validate_offering(self, offering):
        if not offering.state == models.Offering.States.ACTIVE:
            raise rf_exceptions.ValidationError(_('Offering is not available.'))
        return offering

    def validate(self, attrs):
        offering = attrs['offering']
        plan = attrs.get('plan')

        if plan:
            if plan.offering != offering:
                raise rf_exceptions.ValidationError({
                    'plan': _('This plan is not available for selected offering.')
                })

        if offering.options:
            validate_options(offering.options['options'], attrs.get('attributes'))

        return attrs


class OrderSerializer(structure_serializers.PermissionFieldFilteringMixin,
                      core_serializers.AugmentedSerializerMixin,
                      serializers.HyperlinkedModelSerializer):

    state = serializers.ReadOnlyField(source='get_state_display')
    items = OrderItemSerializer(many=True)

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
        new_items = []
        total_cost = 0
        for item in items:
            plan = item.get('plan')
            cost = 0
            if plan:
                cost = plan.unit_price
            total_cost += cost
            order_item = models.OrderItem(
                order=order,
                offering=item['offering'],
                attributes=item.get('attributes', {}),
                plan=plan,
                cost=cost,
            )
            plugins.manager.validate(order_item, self.context['request'])
            new_items.append(order_item)
        models.OrderItem.objects.bulk_create(new_items)
        order.total_cost = total_cost
        order.save()
        return order

    def get_filtered_field_names(self):
        return 'project',


class CustomerOfferingSerializer(serializers.HyperlinkedModelSerializer):
    offering_set = serializers.HyperlinkedRelatedField(
        many=True,
        view_name='marketplace-offering-detail',
        lookup_field='uuid',
        queryset=models.Offering.objects.all()
    )

    class Meta(object):
        model = structure_models.Customer
        fields = ('offering_set',)


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
