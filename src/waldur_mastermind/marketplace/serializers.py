from __future__ import unicode_literals

import datetime

from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db.models import OuterRef, Subquery, Count, IntegerField, Q
from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core.fields import NaturalChoiceField
from waldur_core.core.serializers import GenericRelatedField
from waldur_core.structure import models as structure_models, SupportedServices
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.common.serializers import validate_options
from waldur_mastermind.support import serializers as support_serializers

from . import models, attribute_types, plugins, utils, permissions


class ServiceProviderSerializer(core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.ServiceProvider
        fields = ('url', 'uuid', 'created', 'customer', 'customer_name', 'customer_uuid', 'description',
                  'enable_notifications',)
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
            permissions.can_register_service_provider(self.context['request'], attrs['customer'])
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


class NestedColumnSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.CategoryColumn
        fields = ('index', 'title', 'attribute', 'widget')


class CategorySerializer(core_serializers.AugmentedSerializerMixin,
                         core_serializers.RestrictedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    offering_count = serializers.ReadOnlyField()
    sections = NestedSectionSerializer(many=True, read_only=True)
    columns = NestedColumnSerializer(many=True, read_only=True)

    @staticmethod
    def eager_load(queryset, request):
        offerings = models.Offering.objects \
            .filter(state=models.Offering.States.ACTIVE) \
            .filter(category=OuterRef('pk')) \
            .filter_for_user(request.user)

        allowed_customer_uuid = request.query_params.get('allowed_customer_uuid')
        if allowed_customer_uuid:
            offerings = offerings.filter(Q(shared=True) |
                                         Q(customer__uuid=allowed_customer_uuid) |
                                         Q(allowed_customers__uuid=allowed_customer_uuid))

        offerings = offerings \
            .annotate(count=Count('*'))\
            .values('count')

        # Workaround for Django bug:
        # https://code.djangoproject.com/ticket/28296
        # It allows to remove extra GROUP BY clause from the subquery.
        offerings.query.group_by = []

        offering_count = Subquery(offerings[:1], output_field=IntegerField())
        queryset = queryset.annotate(offering_count=offering_count)
        return queryset.prefetch_related('sections', 'sections__attributes')

    class Meta(object):
        model = models.Category
        fields = ('url', 'uuid', 'title', 'description', 'icon', 'offering_count', 'sections', 'columns')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }


class PlanSerializer(core_serializers.AugmentedSerializerMixin,
                     serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.Plan
        fields = ('url', 'uuid', 'name', 'description', 'unit_price', 'unit', 'offering')
        protected_fields = ('offering',)
        read_ony_fields = ('unit_price',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-plan-detail'},
            'offering': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['offering'].customer)

        return attrs


PriceSerializer = serializers.DecimalField(
    min_value=0,
    max_digits=models.PlanComponent.PRICE_MAX_DIGITS,
    decimal_places=models.PlanComponent.PRICE_DECIMAL_PLACES,
)


class NestedPlanSerializer(core_serializers.AugmentedSerializerMixin,
                           serializers.HyperlinkedModelSerializer):
    prices = serializers.DictField(child=PriceSerializer, write_only=True, required=False)
    quotas = serializers.DictField(child=serializers.IntegerField(min_value=0),
                                   write_only=True, required=False)

    class Meta(object):
        model = models.Plan
        fields = ('url', 'uuid', 'name', 'description', 'unit_price', 'unit', 'prices', 'quotas')
        read_ony_fields = ('unit_price',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-plan-detail'},
        }

    def get_fields(self):
        fields = super(NestedPlanSerializer, self).get_fields()
        method = self.context['view'].request.method
        if method == 'GET':
            fields['prices'] = serializers.SerializerMethodField()
            fields['quotas'] = serializers.SerializerMethodField()
        return fields

    def get_prices(self, plan):
        return {item.component.type: item.price for item in plan.components.all()}

    def get_quotas(self, plan):
        return {item.component.type: item.amount for item in plan.components.all()}


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


class OfferingComponentSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.OfferingComponent
        fields = ('billing_type', 'type', 'name', 'description', 'measured_unit',)
        extra_kwargs = {
            'billing_type': {'required': True},
        }


class OfferingSerializer(core_serializers.AugmentedSerializerMixin,
                         core_serializers.RestrictedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    attributes = serializers.JSONField(required=False)
    options = serializers.JSONField(required=False)
    components = OfferingComponentSerializer(required=False, many=True)
    geolocations = core_serializers.GeoLocationField(required=False)
    order_item_count = serializers.SerializerMethodField()
    plans = NestedPlanSerializer(many=True, required=False)
    screenshots = NestedScreenshotSerializer(many=True, read_only=True)
    state = serializers.ReadOnlyField(source='get_state_display')
    scope = GenericRelatedField(related_models=plugins.manager.get_scope_models, required=False)
    scope_uuid = serializers.ReadOnlyField(source='scope.uuid')

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'created', 'name', 'description', 'full_description',
                  'customer', 'customer_uuid', 'customer_name',
                  'category', 'category_uuid', 'category_title',
                  'rating', 'attributes', 'options', 'components', 'geolocations',
                  'state', 'native_name', 'native_description', 'vendor_details',
                  'thumbnail', 'order_item_count', 'plans', 'screenshots', 'type', 'shared',
                  'scope', 'scope_uuid')
        related_paths = {
            'customer': ('uuid', 'name'),
            'category': ('uuid', 'title'),
        }
        protected_fields = ('customer', 'type', 'scope')
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
                    'attributes': _('Dictionary is expected.'),
                })

            category = attrs.get('category', getattr(self.instance, 'category', None))
            self._validate_attributes(offering_attributes, category)

        self._validate_plans(attrs)
        self._validate_scope(attrs)
        return attrs

    def validate_type(self, offering_type):
        if offering_type not in plugins.manager.backends.keys():
            raise rf_exceptions.ValidationError(_('Invalid value.'))
        return offering_type

    def _validate_attributes(self, offering_attributes, category):
        offering_attribute_keys = offering_attributes.keys()
        required_category_attributes = list(models.Attribute.objects.filter(section__category=category,
                                                                            required=True))
        unfilled_attributes = {attr.key for attr in required_category_attributes} - set(offering_attribute_keys)

        if unfilled_attributes:
            raise rf_exceptions.ValidationError(
                {'attributes': _('Required fields %s are not filled' % list(unfilled_attributes))})

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
        custom_components = attrs.get('components')

        offering_type = attrs.get('type', getattr(self.instance, 'type', None))
        builtin_components = plugins.manager.get_components(offering_type)

        valid_types = set()
        fixed_types = set()

        if builtin_components and custom_components:
            raise serializers.ValidationError({
                'components': _('Extra components are not allowed.')
            })

        elif builtin_components:
            valid_types = {component.type for component in builtin_components}
            fixed_types = {component.type
                           for component in plugins.manager.get_components(offering_type)
                           if component.billing_type == models.OfferingComponent.BillingTypes.FIXED}

        elif custom_components:
            valid_types = {component['type'] for component in custom_components}
            fixed_types = {component['type'] for component in custom_components
                           if component['billing_type'] == models.OfferingComponent.BillingTypes.FIXED}

        for plan in attrs.get('plans', []):
            prices = plan.get('prices', {})
            price_components = set(prices.keys())
            if price_components != valid_types:
                raise serializers.ValidationError({
                    'plans': _('Invalid price components.')
                })

            quotas = plan.get('quotas', {})
            quota_components = set(quotas.keys())
            if quota_components != fixed_types:
                raise serializers.ValidationError({
                    'plans': _('Invalid quota components.')
                })

            plan['unit_price'] = sum(prices[component] * quotas[component]
                                     for component in fixed_types)

    def _validate_scope(self, attrs):
        offering_scope = attrs.get('scope')
        offering_type = attrs.get('type')

        if offering_scope:
            offering_scope_model = plugins.manager.get_scope_model(offering_type)

            if offering_scope_model:
                if not isinstance(offering_scope, offering_scope_model):
                    raise serializers.ValidationError({
                        'scope': _('Invalid scope model.')
                    })

    @transaction.atomic
    def create(self, validated_data):
        plans = validated_data.pop('plans', [])
        custom_components = validated_data.pop('components', [])

        offering = super(OfferingSerializer, self).create(validated_data)
        fixed_components = plugins.manager.get_components(offering.type)

        for component_data in fixed_components:
            models.OfferingComponent.objects.create(
                offering=offering,
                **component_data._asdict()
            )

        for component_data in custom_components:
            models.OfferingComponent.objects.create(offering=offering, **component_data)

        components = {component.type: component for component in offering.components.all()}
        for plan_data in plans:
            self._create_plan(offering, plan_data, components)

        return offering

    def _create_plan(self, offering, plan_data, components):
        quotas = plan_data.pop('quotas', {})
        prices = plan_data.pop('prices', {})
        plan = models.Plan.objects.create(offering=offering, **plan_data)

        for name, component in components.items():
            models.PlanComponent.objects.create(
                plan=plan,
                component=component,
                amount=quotas.get(name) or 0,
                price=prices[name],
            )

    def update(self, instance, validated_data):
        # TODO: Implement support for nested plan update
        validated_data.pop('plans', [])
        validated_data.pop('components', [])
        offering = super(OfferingSerializer, self).update(instance, validated_data)
        return offering


class ComponentQuotaSerializer(serializers.ModelSerializer):
    type = serializers.ReadOnlyField(source='component.type')

    class Meta(object):
        model = models.ComponentQuota
        fields = ('type', 'limit', 'usage')


class BaseItemSerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer,
                         core_serializers.RestrictedSerializerMixin):

    class Meta(object):
        fields = ('offering', 'offering_name', 'offering_uuid',
                  'offering_description', 'offering_thumbnail', 'offering_type',
                  'provider_name', 'provider_uuid',
                  'category_title', 'category_uuid',
                  'plan', 'plan_unit', 'plan_name', 'plan_uuid', 'plan_description',
                  'attributes', 'limits', 'uuid', 'created')
        related_paths = {
            'offering': ('name', 'uuid', 'description', 'type'),
            'plan': ('unit', 'uuid', 'name', 'description')
        }
        protected_fields = ('offering',)
        extra_kwargs = {
            'offering': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
            'plan': {'lookup_field': 'uuid', 'view_name': 'marketplace-plan-detail'},
        }

    provider_name = serializers.ReadOnlyField(source='offering.customer.name')
    provider_uuid = serializers.ReadOnlyField(source='offering.customer.uuid')
    category_title = serializers.ReadOnlyField(source='offering.category.title')
    category_uuid = serializers.ReadOnlyField(source='offering.category.uuid')
    offering_thumbnail = serializers.FileField(source='offering.thumbnail', read_only=True)

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

        limits = attrs.get('limits')
        if limits:
            valid_component_types = offering.components \
                .filter(billing_type=models.OfferingComponent.BillingTypes.USAGE) \
                .values_list('type', flat=True)
            invalid_types = set(limits.keys()) - set(valid_component_types)
            if invalid_types:
                raise ValidationError({'limits': _('Invalid types: %s') % ', '.join(invalid_types)})
        return attrs


class BaseRequestSerializer(BaseItemSerializer):
    type = NaturalChoiceField(
        choices=models.RequestTypeMixin.Types.CHOICES,
        required=False,
        default=models.RequestTypeMixin.Types.CREATE,
    )

    class Meta(BaseItemSerializer.Meta):
        fields = BaseItemSerializer.Meta.fields + ('type',)


class OrderItemSerializer(BaseRequestSerializer):
    class Meta(BaseRequestSerializer.Meta):
        model = models.OrderItem
        fields = BaseRequestSerializer.Meta.fields + (
            'customer_name', 'customer_uuid',
            'project_name', 'project_uuid',
            'resource_uuid', 'resource_type',
            'cost', 'state', 'marketplace_resource_uuid',
        )

        read_only_fields = ('cost', 'state')
        protected_fields = ('offering', 'plan')

    customer_name = serializers.ReadOnlyField(source='order.project.customer.name')
    customer_uuid = serializers.ReadOnlyField(source='order.project.customer.uuid')
    project_name = serializers.ReadOnlyField(source='order.project.name')
    project_uuid = serializers.ReadOnlyField(source='order.project.uuid')
    marketplace_resource_uuid = serializers.ReadOnlyField(source='resource.uuid')
    resource_uuid = serializers.ReadOnlyField(source='resource.backend_uuid')
    resource_type = serializers.ReadOnlyField(source='resource.backend_type')
    state = serializers.ReadOnlyField(source='get_state_display')
    limits = serializers.DictField(child=serializers.IntegerField(), required=False)


class CartItemSerializer(BaseRequestSerializer):
    limits = serializers.DictField(child=serializers.IntegerField(), required=False)

    class Meta(BaseRequestSerializer.Meta):
        model = models.CartItem
        fields = BaseRequestSerializer.Meta.fields + ('estimate',)

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super(CartItemSerializer, self).create(validated_data)


class CartSubmitSerializer(serializers.Serializer):
    project = serializers.HyperlinkedRelatedField(
        queryset=structure_models.Project.objects.all(),
        view_name='project-detail',
        lookup_field='uuid',
        required=True,
    )

    def get_fields(self):
        fields = super(CartSubmitSerializer, self).get_fields()
        project_field = fields['project']
        project_field.queryset = filter_queryset_for_user(
            project_field.queryset, self.context['request'].user)
        return fields

    @transaction.atomic()
    def create(self, validated_data):
        user = self.context['request'].user

        items = models.CartItem.objects.filter(user=user)
        if items.count() == 0:
            raise serializers.ValidationError(_('Shopping cart is empty'))

        project = validated_data['project']
        order = models.Order.objects.create(project=project, created_by=user)

        for item in items:
            try:
                order_item = order.add_item(
                    offering=item.offering,
                    attributes=item.attributes,
                    plan=item.plan,
                    limits=item.limits,
                    type=item.type,
                )
            except ValidationError as e:
                raise rf_exceptions.ValidationError(e)
            plugins.manager.validate(order_item, self.context['request'])

        order.init_total_cost()
        order.save()

        items.delete()
        return order


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
                  'project', 'state', 'items', 'total_cost', 'file')
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

    file = serializers.SerializerMethodField()

    def get_file(self, obj):
        if not obj.has_file():
            return None

        return reverse('marketplace-order-pdf',
                       kwargs={'uuid': obj.uuid},
                       request=self.context['request'])

    @transaction.atomic
    def create(self, validated_data):
        user = self.context['request'].user
        validated_data['created_by'] = user
        items = validated_data.pop('items')
        order = super(OrderSerializer, self).create(validated_data)
        for item in items:
            try:
                order_item = order.add_item(
                    offering=item['offering'],
                    plan=item.get('plan'),
                    attributes=item.get('attributes', {}),
                    limits=item.get('limits'),
                    type=item.get('type'),
                )
            except ValidationError as e:
                raise rf_exceptions.ValidationError(e)
            plugins.manager.validate(order_item, self.context['request'])

        order.init_total_cost()
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


class ResourceSerializer(BaseItemSerializer):
    class Meta(BaseItemSerializer.Meta):
        model = models.Resource
        fields = BaseItemSerializer.Meta.fields + ('state', 'resource_uuid', 'resource_type')

    state = serializers.ReadOnlyField(source='get_state_display')
    resource_uuid = serializers.ReadOnlyField(source='backend_uuid')
    resource_type = serializers.ReadOnlyField(source='backend_type')


class ServiceProviderSignatureSerializer(serializers.Serializer):
    signature = serializers.CharField()
    customer = serializers.SlugRelatedField(queryset=structure_models.Customer.objects.all(), slug_field='uuid')
    data = serializers.JSONField()
    dry_run = serializers.BooleanField(default=False, required=False)

    def validate(self, attrs):
        customer = attrs['customer']
        service_provider = getattr(customer, 'serviceprovider', None)
        api_secret_code = service_provider and service_provider.api_secret_code

        if not api_secret_code:
            raise rf_exceptions.ValidationError(_('API secret code is not set.'))

        if utils.check_api_signature(data=attrs['data'],
                                     api_secret_code=api_secret_code,
                                     signature=attrs['signature']):
            return attrs
        else:
            raise rf_exceptions.ValidationError(_('Invalid signature.'))


class PublicComponentUsageSerializer(serializers.Serializer):
    resource = serializers.SlugRelatedField(queryset=models.Resource.objects.all(), slug_field='uuid')
    date = serializers.DateField()
    type = serializers.CharField()
    amount = serializers.IntegerField()

    def validate(self, attrs):
        resource = attrs['resource']
        date = attrs['date']
        plan = resource.plan
        if not plan:
            raise rf_exceptions.ValidationError({
                'resource': _('Resource does not have billing plan.')
            })

        offering = plan.offering

        if date > datetime.date.today():
            raise rf_exceptions.ValidationError({'date': _('Invalid date value.')})

        try:
            component = models.OfferingComponent.objects.get(
                offering=offering,
                type=attrs['type'],
                billing_type=models.OfferingComponent.BillingTypes.USAGE,
            )
        except models.OfferingComponent.DoesNotExist:
            raise rf_exceptions.ValidationError(_('Component "%s" is not found.') % attrs['type'])

        if models.ComponentUsage.objects.filter(resource=resource,
                                                component=component,
                                                date=attrs['date']).exists():
            raise rf_exceptions.ValidationError({
                'date': _('Component usage for provided billing period already exists.')
            })

        attrs['component'] = component

        if plan.unit == UnitPriceMixin.Units.PER_MONTH:
            attrs['date'] = datetime.date(year=date.year, month=date.month, day=1)

        if plan.unit == UnitPriceMixin.Units.PER_HALF_MONTH:
            if date.day < 16:
                attrs['date'] = datetime.date(year=date.year, month=date.month, day=1)
            else:
                attrs['date'] = datetime.date(year=date.year, month=date.month, day=16)

        return attrs


class PublicListComponentUsageSerializer(serializers.Serializer):
    usages = PublicComponentUsageSerializer(many=True)


def get_is_service_provider(serializer, scope):
    customer = structure_permissions._get_customer(scope)
    return models.ServiceProvider.objects.filter(customer=customer).exists()


def add_service_provider(sender, fields, **kwargs):
    fields['is_service_provider'] = serializers.SerializerMethodField()
    setattr(sender, 'get_is_service_provider', get_is_service_provider)


def get_marketplace_offering_uuid(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.uuid
    except ObjectDoesNotExist:
        return


def add_marketplace_offering(sender, fields, **kwargs):
    fields['marketplace_offering_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_offering_uuid', get_marketplace_offering_uuid)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_service_provider,
)

core_signals.pre_serializer_fields.connect(
    sender=support_serializers.OfferingSerializer,
    receiver=add_marketplace_offering,
)

for resource_serializer in SupportedServices.get_resource_serializers():
    core_signals.pre_serializer_fields.connect(
        sender=resource_serializer,
        receiver=add_marketplace_offering,
    )
