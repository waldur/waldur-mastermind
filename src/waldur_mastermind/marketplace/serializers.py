from __future__ import unicode_literals

import datetime
import logging

import jwt
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db import transaction
from django.db.models import OuterRef, Subquery, Count, IntegerField
from django.utils.translation import ugettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core import utils as core_utils
from waldur_core.core.fields import NaturalChoiceField
from waldur_core.core.serializers import GenericRelatedField
from waldur_core.quotas.serializers import BasicQuotaSerializer
from waldur_core.structure import models as structure_models, SupportedServices
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.tasks import connect_shared_settings
from waldur_mastermind.common.serializers import validate_options
from waldur_mastermind.marketplace.utils import validate_order_item
from waldur_mastermind.support import serializers as support_serializers

from . import models, attribute_types, plugins, utils, permissions, tasks

logger = logging.getLogger(__name__)


class ServiceProviderSerializer(core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.ServiceProvider
        fields = (
            'url', 'uuid', 'created', 'description', 'enable_notifications',
            'customer', 'customer_name', 'customer_uuid', 'customer_image',
            'customer_abbreviation', 'customer_native_name',
        )
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation')
        }
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-service-provider-detail'},
            'customer': {'lookup_field': 'uuid'},
        }

    customer_image = serializers.ImageField(source='customer.image', read_only=True)

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
        fields = ('key', 'title', 'type', 'options', 'required', 'default')


class NestedSectionSerializer(serializers.ModelSerializer):
    attributes = NestedAttributeSerializer(many=True, read_only=True)

    class Meta(object):
        model = models.Section
        fields = ('key', 'title', 'attributes', 'is_standalone')


class NestedColumnSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.CategoryColumn
        fields = ('index', 'title', 'attribute', 'widget')


class CategoryComponentSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.CategoryComponent
        fields = ('type', 'name', 'description', 'measured_unit')


class CategorySerializer(core_serializers.AugmentedSerializerMixin,
                         core_serializers.RestrictedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    offering_count = serializers.ReadOnlyField()
    sections = NestedSectionSerializer(many=True, read_only=True)
    columns = NestedColumnSerializer(many=True, read_only=True)
    components = CategoryComponentSerializer(many=True, read_only=True)

    @staticmethod
    def eager_load(queryset, request):
        offerings = models.Offering.objects \
            .filter(state=models.Offering.States.ACTIVE) \
            .filter(category=OuterRef('pk')) \
            .filter_for_user(request.user)

        allowed_customer_uuid = request.query_params.get('allowed_customer_uuid')
        if allowed_customer_uuid and core_utils.is_uuid_like(allowed_customer_uuid):
            offerings = offerings.filter_for_customer(allowed_customer_uuid)

        project_uuid = request.query_params.get('project_uuid')
        if project_uuid and core_utils.is_uuid_like(project_uuid):
            offerings = offerings.filter_for_project(project_uuid)

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
        fields = ('url', 'uuid', 'title', 'description', 'icon', 'offering_count',
                  'sections', 'columns', 'components')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }


PriceSerializer = serializers.DecimalField(
    min_value=0,
    max_digits=models.PlanComponent.PRICE_MAX_DIGITS,
    decimal_places=models.PlanComponent.PRICE_DECIMAL_PLACES,
)


class BasePlanSerializer(core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer):
    prices = serializers.DictField(child=PriceSerializer, write_only=True, required=False)
    quotas = serializers.DictField(child=serializers.IntegerField(min_value=0),
                                   write_only=True, required=False)

    class Meta(object):
        model = models.Plan
        fields = ('url', 'uuid', 'name', 'description', 'unit_price', 'unit',
                  'prices', 'quotas', 'max_amount', 'archived', 'is_active')
        read_ony_fields = ('unit_price', 'archived')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-plan-detail'},
        }

    def get_fields(self):
        fields = super(BasePlanSerializer, self).get_fields()
        method = self.context['view'].request.method
        if method == 'GET':
            fields['prices'] = serializers.SerializerMethodField()
            fields['quotas'] = serializers.SerializerMethodField()
        return fields

    def get_prices(self, plan):
        return {item.component.type: item.price for item in plan.components.all()}

    def get_quotas(self, plan):
        return {item.component.type: item.amount for item in plan.components.all()}


class PlanDetailsSerializer(BasePlanSerializer):
    class Meta(BasePlanSerializer.Meta):
        model = models.Plan
        fields = BasePlanSerializer.Meta.fields + ('offering',)
        protected_fields = ('offering',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-plan-detail'},
            'offering': {'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['offering'].customer)

        return attrs


class PlanUsageRequestSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(required=False)
    customer_provider_uuid = serializers.UUIDField(required=False)
    o = serializers.ChoiceField(choices=(
        'usage', 'limit', 'remaining',
        '-usage', '-limit', '-remaining',
    ), required=False)


class PlanUsageResponseSerializer(serializers.Serializer):
    plan_uuid = serializers.ReadOnlyField(source='uuid')
    plan_name = serializers.ReadOnlyField(source='name')

    limit = serializers.ReadOnlyField()
    usage = serializers.ReadOnlyField()
    remaining = serializers.ReadOnlyField()

    offering_uuid = serializers.ReadOnlyField(source='offering.uuid')
    offering_name = serializers.ReadOnlyField(source='offering.name')

    customer_provider_uuid = serializers.ReadOnlyField(source='offering.customer.uuid')
    customer_provider_name = serializers.ReadOnlyField(source='offering.customer.name')


class NestedScreenshotSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.Screenshot
        fields = ('name', 'description', 'image', 'thumbnail')


class NestedOfferingFileSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = models.OfferingFile
        fields = ('name', 'created', 'file',)


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
    'date',
    'time',
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
        fields = ('billing_type', 'type', 'name', 'description', 'measured_unit',
                  'limit_period', 'limit_amount', 'disable_quotas', 'product_code', 'article_code')
        extra_kwargs = {
            'billing_type': {'required': True},
        }


class OfferingDetailsSerializer(core_serializers.AugmentedSerializerMixin,
                                core_serializers.RestrictedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):

    attributes = serializers.JSONField(required=False)
    options = serializers.JSONField(required=False)
    components = OfferingComponentSerializer(required=False, many=True)
    geolocations = core_serializers.GeoLocationField(required=False)
    order_item_count = serializers.SerializerMethodField()
    plans = BasePlanSerializer(many=True, required=False)
    screenshots = NestedScreenshotSerializer(many=True, read_only=True)
    state = serializers.ReadOnlyField(source='get_state_display')
    scope = GenericRelatedField(read_only=True)
    scope_uuid = serializers.ReadOnlyField(source='scope.uuid')
    files = NestedOfferingFileSerializer(many=True, read_only=True)
    quotas = serializers.SerializerMethodField()

    class Meta(object):
        model = models.Offering
        fields = ('url', 'uuid', 'created', 'name', 'description', 'full_description', 'terms_of_service',
                  'customer', 'customer_uuid', 'customer_name',
                  'category', 'category_uuid', 'category_title',
                  'rating', 'attributes', 'options', 'components', 'geolocations',
                  'state', 'native_name', 'native_description', 'vendor_details',
                  'thumbnail', 'order_item_count', 'plans', 'screenshots', 'type', 'shared', 'billable',
                  'scope', 'scope_uuid', 'files', 'quotas')
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

    def get_quotas(self, offering):
        if offering.scope and hasattr(offering.scope, 'quotas'):
            return BasicQuotaSerializer(offering.scope.quotas, many=True, context=self.context).data


class OfferingModifySerializer(OfferingDetailsSerializer):

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(self.context['request'], None, attrs['customer'])

        self._validate_attributes(attrs)
        self._validate_plans(attrs)

        return attrs

    def validate_type(self, offering_type):
        if offering_type not in plugins.manager.backends.keys():
            raise rf_exceptions.ValidationError(_('Invalid value.'))
        return offering_type

    def _validate_attributes(self, attrs):
        category = attrs.get('category')
        if category is None and self.instance:
            category = self.instance.category

        attributes = attrs.get('attributes')
        if attributes is not None and not isinstance(attributes, dict):
            raise rf_exceptions.ValidationError({
                'attributes': _('Dictionary is expected.'),
            })

        if attributes is None and self.instance:
            attributes = self.instance.attributes

        category_attributes = models.Attribute.objects.filter(section__category=category)
        required_attributes = category_attributes.filter(required=True).values_list('key', flat=True)
        missing_attributes = set(required_attributes) - (set(attributes.keys()) if attributes else set())

        if missing_attributes:
            raise rf_exceptions.ValidationError({
                'attributes': _('These attributes are required: %s' % ', '.join(sorted(missing_attributes)))
            })

        for attribute in category_attributes:
            value = attributes.get(attribute.key)
            if value is None:
                # Use default attribute value if it is defined
                if attribute.default is not None:
                    attributes[attribute.key] = attribute.default
                continue

            validator = attribute_types.get_attribute_type(attribute.type)
            if not validator:
                continue

            try:
                validator.validate(value, list(attribute.options.values_list('key', flat=True)))
            except ValidationError as e:
                raise rf_exceptions.ValidationError({attribute.key: e.message})

    def validate_options(self, options):
        serializer = OfferingOptionsSerializer(data=options)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def _validate_plans(self, attrs):
        custom_components = attrs.get('components')
        if not custom_components and self.instance:
            custom_components = self.instance.components.all().values()

        offering_type = attrs.get('type', getattr(self.instance, 'type', None))
        builtin_components = plugins.manager.get_components(offering_type)

        valid_types = set()
        fixed_types = set()

        if builtin_components and attrs.get('components'):
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
            # Zero is default value for plan component amount so it is okay to skip it
            quota_components = {key for (key, value) in quotas.items() if value != 0}
            if quota_components != fixed_types:
                raise serializers.ValidationError({
                    'plans': _('Invalid quota components.')
                })

            plan['unit_price'] = sum(prices[component] * quotas[component]
                                     for component in fixed_types)

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

    def _create_components(self, offering, custom_components):
        fixed_components = plugins.manager.get_components(offering.type)

        for component_data in fixed_components:
            models.OfferingComponent.objects.create(
                offering=offering,
                **component_data._asdict()
            )

        for component_data in custom_components:
            models.OfferingComponent.objects.create(offering=offering, **component_data)

    def _create_plans(self, offering, plans):
        components = {component.type: component for component in offering.components.all()}
        for plan_data in plans:
            self._create_plan(offering, plan_data, components)


class OfferingCreateSerializer(OfferingModifySerializer):
    class Meta(OfferingModifySerializer.Meta):
        fields = OfferingModifySerializer.Meta.fields + ('service_attributes',)

    service_attributes = serializers.JSONField(required=False, write_only=True)

    def validate_plans(self, plans):
        if len(plans) < 1:
            raise serializers.ValidationError({
                'plans': _('At least one plan should be specified.')
            })
        return plans

    @transaction.atomic
    def create(self, validated_data):
        plans = validated_data.pop('plans', [])
        custom_components = validated_data.pop('components', [])

        offering_type = validated_data.get('type')
        service_type = plugins.manager.get_service_type(offering_type)
        if service_type:
            validated_data = self._create_service(service_type, validated_data)

        offering = super(OfferingCreateSerializer, self).create(validated_data)
        self._create_components(offering, custom_components)
        self._create_plans(offering, plans)

        return offering

    def _create_service(self, service_type, validated_data):
        """
        Marketplace offering model does not accept service_attributes field as is,
        therefore we should remove it from validated_data and create service settings object.
        Then we need to specify created object and offering's scope.
        """
        name = validated_data['name']
        service_attributes = validated_data.pop('service_attributes', {})
        if not service_attributes:
            raise ValidationError({
                'service_attributes': _('This field is required.')
            })
        payload = dict(
            name=name,
            # It is expected that customer URL is passed to the service settings serializer
            customer=self.initial_data['customer'],
            type=service_type,
            **service_attributes
        )
        serializer_class = SupportedServices.get_service_serializer_for_key(service_type)
        serializer = serializer_class(data=payload, context=self.context)
        serializer.is_valid(raise_exception=True)
        service = serializer.save()
        # Usually we don't allow users to create new shared service settings via REST API.
        # That's shared flag is marked as read-only in service settings serializer.
        # But shared offering should be created with shared service settings.
        # That's why we set it to shared only after service settings object is created.
        if validated_data.get('shared'):
            service.settings.shared = True
            service.settings.save()
            # Usually connect shared settings task is called when service is created.
            # But as we set shared flag after serializer has been executed,
            # we need to connect shared settings manually.
            connect_shared_settings(service.settings)
        validated_data['scope'] = service.settings
        return validated_data


class PlanUpdateSerializer(BasePlanSerializer):

    class Meta(BasePlanSerializer.Meta):
        extra_kwargs = {
            'uuid': {'read_only': False},
        }


class OfferingUpdateSerializer(OfferingModifySerializer):

    plans = PlanUpdateSerializer(many=True, required=False, write_only=True)

    def _update_components(self, instance, components):
        resources_exist = models.Resource.objects.filter(offering=instance).exists()

        old_components = {
            component.type: component
            for component in instance.components.all()
        }

        new_components = {
            component['type']: models.OfferingComponent(offering=instance, **component)
            for component in components
        }

        removed_components = set(old_components.keys()) - set(new_components.keys())
        added_components = set(new_components.keys()) - set(old_components.keys())
        updated_components = set(new_components.keys()) & set(old_components.keys())

        if removed_components:
            if resources_exist:
                raise serializers.ValidationError({
                    'components': _('These components cannot be removed because they are already used: %s') %
                    ', '.join(removed_components)
                })
            else:
                models.OfferingComponent.objects.filter(type__in=removed_components).delete()

        for key in added_components:
            new_components[key].save()

        COMPONENT_KEYS = (
            'name', 'description',
            'billing_type', 'measured_unit',
            'limit_period', 'limit_amount', 'disable_quotas',
            'product_code', 'article_code',
        )

        for component_key in updated_components:
            new_component = new_components[component_key]
            old_component = old_components[component_key]
            for key in COMPONENT_KEYS:
                setattr(old_component, key, getattr(new_component, key))
            old_component.save()

    def _update_plan_components(self, old_plan, new_plan):
        new_quotas = new_plan.get('quotas', {})
        new_prices = new_plan.get('prices', {})

        new_keys = set(new_quotas.keys()) | set(new_prices.keys())
        old_keys = set(old_plan.components.values_list('component__type', flat=True))

        for key in new_keys - old_keys:
            component = old_plan.offering.components.get(type=key)
            models.PlanComponent.objects.create(plan=old_plan, component=component)

    def _update_quotas(self, old_plan, new_plan):
        new_quotas = new_plan.get('quotas', {})
        new_prices = new_plan.get('prices', {})
        component_map = {
            component.component.type: component
            for component in old_plan.components.all()
        }
        for key, old_component in component_map.items():
            new_amount = new_quotas.get(key, 0)
            if old_component.amount != new_amount:
                old_component.amount = new_amount
                old_component.save(update_fields=['amount'])

            new_price = new_prices.get(key, 0)
            if old_component.price != new_price:
                old_component.price = new_price
                old_component.save(update_fields=['price'])

    def _update_plan_details(self, old_plan, new_plan):
        PLAN_FIELDS = (
            'name', 'description',
            'unit', 'max_amount',
            'product_code', 'article_code',
        )

        for key in PLAN_FIELDS:
            if key in new_plan:
                setattr(old_plan, key, new_plan.get(key))
        old_plan.save()

    def _update_plans(self, offering, new_plans):
        old_plans = offering.plans.all()
        old_ids = set(old_plans.values_list('uuid', flat=True))

        new_map = {plan['uuid']: plan for plan in new_plans if 'uuid' in plan}
        added_plans = [plan for plan in new_plans if 'uuid' not in plan]

        removed_ids = set(old_ids) - set(new_map.keys())
        updated_ids = set(new_map.keys()) & set(old_ids)

        removed_plans = models.Plan.objects.filter(uuid__in=removed_ids).exclude(archived=True)
        updated_plans = {plan.uuid: plan for plan in models.Plan.objects.filter(uuid__in=updated_ids)}

        for plan_uuid, old_plan in updated_plans.items():
            new_plan = new_map[plan_uuid]
            self._update_plan_details(old_plan, new_plan)
            self._update_plan_components(old_plan, new_plan)
            self._update_quotas(old_plan, new_plan)

        if added_plans:
            self._create_plans(offering, added_plans)

        for plan in removed_plans:
            plan.archived = True
            plan.save()

    @transaction.atomic
    def update(self, instance, validated_data):
        if 'components' in validated_data:
            components = validated_data.pop('components', [])
            self._update_components(instance, components)
        if 'plans' in validated_data:
            new_plans = validated_data.pop('plans', [])
            self._update_plans(instance, new_plans)
        offering = super(OfferingUpdateSerializer, self).update(instance, validated_data)
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
                  'offering_terms_of_service', 'offering_shared', 'offering_billable',
                  'provider_name', 'provider_uuid',
                  'category_title', 'category_uuid',
                  'plan', 'plan_unit', 'plan_name', 'plan_uuid', 'plan_description',
                  'attributes', 'limits', 'uuid', 'created')
        related_paths = {
            'offering': ('name', 'uuid', 'description', 'type', 'terms_of_service', 'shared', 'billable'),
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
        offering = attrs.get('offering')
        plan = attrs.get('plan')

        if not offering:
            if not self.instance:
                raise rf_exceptions.ValidationError({
                    'offering': _('This field is required.')
                })
            offering = self.instance.offering

        if plan:
            if plan.offering != offering:
                raise rf_exceptions.ValidationError({
                    'plan': _('This plan is not available for selected offering.')
                })

            validate_plan(plan)

        if offering.options:
            validate_options(offering.options['options'], attrs.get('attributes'))

        limits = attrs.get('limits')
        if limits:
            valid_component_types = offering.components \
                .filter(billing_type=models.OfferingComponent.BillingTypes.USAGE) \
                .exclude(disable_quotas=True) \
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


class NestedOrderItemSerializer(BaseRequestSerializer):
    class Meta(BaseRequestSerializer.Meta):
        model = models.OrderItem
        fields = BaseRequestSerializer.Meta.fields + (
            'resource_uuid', 'resource_type', 'resource_name',
            'cost', 'state', 'marketplace_resource_uuid', 'error_message',
        )

        read_only_fields = ('cost', 'state', 'error_message')
        protected_fields = ('offering', 'plan')

    marketplace_resource_uuid = serializers.ReadOnlyField(source='resource.uuid')
    resource_name = serializers.ReadOnlyField(source='resource.name')
    resource_uuid = serializers.ReadOnlyField(source='resource.backend_uuid')
    resource_type = serializers.ReadOnlyField(source='resource.backend_type')
    state = serializers.ReadOnlyField(source='get_state_display')
    limits = serializers.DictField(child=serializers.IntegerField(), required=False)

    def get_fields(self):
        fields = super(BaseItemSerializer, self).get_fields()
        method = self.context['view'].request.method
        if method == 'GET':
            fields['attributes'] = serializers.ReadOnlyField(source='safe_attributes')
        return fields


class OrderItemDetailsSerializer(NestedOrderItemSerializer):
    class Meta(NestedOrderItemSerializer.Meta):
        fields = NestedOrderItemSerializer.Meta.fields + (
            'order_uuid', 'order_approved_at', 'order_approved_by',
            'created_by_full_name', 'created_by_civil_number',
            'customer_name', 'customer_uuid',
            'project_name', 'project_uuid',
            'old_plan_name', 'new_plan_name',
            'old_plan_uuid', 'new_plan_uuid',
            'old_cost_estimate', 'new_cost_estimate',
            'can_terminate',
        )

    order_uuid = serializers.ReadOnlyField(source='order.uuid')
    order_approved_at = serializers.ReadOnlyField(source='order.approved_at')
    order_approved_by = serializers.ReadOnlyField(source='order.approved_by.full_name')

    created_by_full_name = serializers.ReadOnlyField(source='order.created_by.full_name')
    created_by_civil_number = serializers.ReadOnlyField(source='order.created_by.civil_number')

    customer_name = serializers.ReadOnlyField(source='order.project.customer.name')
    customer_uuid = serializers.ReadOnlyField(source='order.project.customer.uuid')

    project_name = serializers.ReadOnlyField(source='order.project.name')
    project_uuid = serializers.ReadOnlyField(source='order.project.uuid')

    old_plan_name = serializers.ReadOnlyField(source='old_plan.name')
    new_plan_name = serializers.ReadOnlyField(source='plan.name')

    old_plan_uuid = serializers.ReadOnlyField(source='old_plan.uuid')
    new_plan_uuid = serializers.ReadOnlyField(source='plan.uuid')

    old_cost_estimate = serializers.ReadOnlyField(source='resource.cost')
    new_cost_estimate = serializers.ReadOnlyField(source='cost')

    can_terminate = serializers.SerializerMethodField()

    def get_can_terminate(self, order_item):
        if not plugins.manager.can_terminate_order_item(order_item.offering.type):
            return False

        if order_item.state not in (models.OrderItem.States.PENDING, models.OrderItem.States.EXECUTING):
            return False

        return True


class CartItemSerializer(BaseRequestSerializer):
    limits = serializers.DictField(child=serializers.IntegerField(), required=False)
    estimate = serializers.ReadOnlyField(source='cost')

    class Meta(BaseRequestSerializer.Meta):
        model = models.CartItem
        fields = BaseRequestSerializer.Meta.fields + ('estimate',)

    @transaction.atomic
    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        item = super(CartItemSerializer, self).create(validated_data)
        item.init_cost()
        item.save(update_fields=['cost'])
        return item


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
        order = create_order(project, user, items, self.context['request'])
        items.delete()
        return order


def check_availability_of_auto_approving(items, user, project):
    if user.is_staff:
        return True

    # Skip approval of private offering for project users
    if all(item.offering.is_private for item in items):
        return structure_permissions._has_admin_access(user, project)

    return permissions.user_can_approve_order(user, project)


def create_order(project, user, items, request):
    order_params = dict(project=project, created_by=user)
    order = models.Order.objects.create(**order_params)

    for item in items:
        if item.type in (models.OrderItem.Types.UPDATE, models.OrderItem.Types.TERMINATE) and \
                item.resource and models.OrderItem.objects.filter(
            resource=item.resource,
            state__in=(models.OrderItem.States.PENDING, models.OrderItem.States.EXECUTING)
        ).exists():
            raise rf_exceptions.ValidationError(_('Pending order item for resource already exists.'))

        try:
            order_item = order.add_item(
                offering=item.offering,
                attributes=item.attributes,
                resource=getattr(item, 'resource', None),  # cart item does not have resource
                plan=item.plan,
                old_plan=getattr(item, 'old_plan', None),  # cart item does not have old plan
                limits=item.limits,
                type=item.type,
            )
        except ValidationError as e:
            raise rf_exceptions.ValidationError(e)
        validate_order_item(order_item, request)

    order.init_total_cost()
    order.save()

    if check_availability_of_auto_approving(items, user, project):
        tasks.approve_order(order, user)
    else:
        transaction.on_commit(lambda: tasks.notify_order_approvers.delay(order.uuid))

    return order


class OrderSerializer(structure_serializers.PermissionFieldFilteringMixin,
                      core_serializers.AugmentedSerializerMixin,
                      serializers.HyperlinkedModelSerializer):

    state = serializers.ReadOnlyField(source='get_state_display')
    items = NestedOrderItemSerializer(many=True)

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
        request = self.context['request']
        project = validated_data['project']
        items = [
            models.OrderItem(
                offering=item['offering'],
                plan=item.get('plan'),
                attributes=item.get('attributes', {}),
                limits=item.get('limits', {}),
                type=item.get('type'),
            )
            for item in validated_data['items']
        ]
        return create_order(project, request.user, items, request)

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
        fields = BaseItemSerializer.Meta.fields + (
            'scope', 'state', 'resource_uuid', 'resource_type',
            'project', 'project_uuid', 'project_name',
            'customer_uuid', 'customer_name',
            'offering_uuid', 'offering_name',
            'backend_metadata', 'is_usage_based', 'name',
        )
        read_only_fields = ('backend_metadata', 'scope',)

    state = serializers.ReadOnlyField(source='get_state_display')
    scope = core_serializers.GenericRelatedField()
    resource_uuid = serializers.ReadOnlyField(source='backend_uuid')
    resource_type = serializers.ReadOnlyField(source='backend_type')
    project = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name='project-detail',
        read_only=True,
    )
    project_uuid = serializers.ReadOnlyField(source='project.uuid')
    project_name = serializers.ReadOnlyField(source='project.name')
    customer_uuid = serializers.ReadOnlyField(source='project.customer.uuid')
    customer_name = serializers.ReadOnlyField(source='project.customer.name')
    offering_uuid = serializers.ReadOnlyField(source='offering.uuid')
    offering_name = serializers.ReadOnlyField(source='offering.name')
    # If resource is usage-based, frontend would render button to show and report usage
    is_usage_based = serializers.ReadOnlyField(source='offering.is_usage_based')


class ResourceSwitchPlanSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.Resource
        fields = ('plan',)

    plan = serializers.HyperlinkedRelatedField(
        view_name='marketplace-plan-detail',
        lookup_field='uuid',
        queryset=models.Plan.objects.all(),
        required=True,
    )

    def validate(self, attrs):
        plan = attrs['plan']
        resource = self.context['view'].get_object()

        if plan.offering != resource.offering:
            raise rf_exceptions.ValidationError({
                'plan': _('Plan is not available for this offering.')
            })

        validate_plan(plan)
        return attrs


class BaseComponentSerializer(serializers.Serializer):
    type = serializers.ReadOnlyField(source='component.type')
    name = serializers.ReadOnlyField(source='component.name')
    measured_unit = serializers.ReadOnlyField(source='component.measured_unit')


class CategoryComponentUsageSerializer(core_serializers.RestrictedSerializerMixin,
                                       BaseComponentSerializer,
                                       serializers.ModelSerializer):
    category_title = serializers.ReadOnlyField(source='component.category.title')
    category_uuid = serializers.ReadOnlyField(source='component.category.uuid')
    scope = GenericRelatedField(related_models=(structure_models.Project, structure_models.Customer))

    class Meta(object):
        model = models.CategoryComponentUsage
        fields = ('name', 'type', 'measured_unit', 'category_title', 'category_uuid',
                  'date', 'reported_usage', 'fixed_usage', 'scope')


class BaseComponentUsageSerializer(BaseComponentSerializer, serializers.ModelSerializer):
    class Meta(object):
        model = models.ComponentUsage
        fields = (
            'uuid', 'created', 'description',
            'type', 'name', 'measured_unit', 'usage', 'date',
        )


class ComponentUsageSerializer(BaseComponentUsageSerializer):
    resource_name = serializers.ReadOnlyField(source='resource.name')
    resource_uuid = serializers.ReadOnlyField(source='resource.uuid')

    offering_name = serializers.ReadOnlyField(source='resource.offering.name')
    offering_uuid = serializers.ReadOnlyField(source='resource.offering.uuid')

    project_name = serializers.ReadOnlyField(source='resource.project.name')
    project_uuid = serializers.ReadOnlyField(source='resource.project.uuid')

    customer_name = serializers.ReadOnlyField(source='resource.project.customer.name')
    customer_uuid = serializers.ReadOnlyField(source='resource.project.customer.uuid')

    class Meta(BaseComponentUsageSerializer.Meta):
        fields = BaseComponentUsageSerializer.Meta.fields + (
            'resource_name', 'resource_uuid',
            'offering_name', 'offering_uuid',
            'project_name', 'project_uuid',
            'customer_name', 'customer_uuid',
        )


class ResourcePlanPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ResourcePlanPeriod
        fields = ('uuid', 'plan_name', 'plan_uuid', 'start', 'end', 'components')

    plan_name = serializers.ReadOnlyField(source='plan.name')
    plan_uuid = serializers.ReadOnlyField(source='plan.uuid')
    components = BaseComponentUsageSerializer(many=True)


class ServiceProviderSignatureSerializer(serializers.Serializer):
    customer = serializers.SlugRelatedField(queryset=structure_models.Customer.objects.all(), slug_field='uuid')
    data = serializers.CharField()
    dry_run = serializers.BooleanField(default=False, required=False)

    def validate(self, attrs):
        customer = attrs['customer']
        service_provider = getattr(customer, 'serviceprovider', None)
        api_secret_code = service_provider and service_provider.api_secret_code

        if not api_secret_code:
            raise rf_exceptions.ValidationError(_('API secret code is not set.'))

        try:
            data = utils.decode_api_data(attrs['data'], api_secret_code)
            attrs['data'] = data
            return attrs
        except jwt.exceptions.DecodeError:
            raise rf_exceptions.ValidationError(_('Signature verification failed.'))


class ComponentUsageItemSerializer(serializers.Serializer):
    type = serializers.CharField()
    amount = serializers.IntegerField()
    description = serializers.CharField(required=False, allow_blank=True)


class ComponentUsageCreateSerializer(serializers.Serializer):
    usages = ComponentUsageItemSerializer(many=True)
    plan_period = serializers.SlugRelatedField(queryset=models.ResourcePlanPeriod.objects.all(), slug_field='uuid')

    def validate_plan_period(self, plan_period):
        date = datetime.date.today()
        if plan_period.end and plan_period.end < core_utils.month_start(date):
            raise serializers.ValidationError(_('Billing period is closed.'))
        return plan_period

    def validate(self, attrs):
        attrs = super(ComponentUsageCreateSerializer, self).validate(attrs)
        plan_period = attrs['plan_period']
        resource = plan_period.resource
        offering = resource.plan.offering

        if resource.state == models.Resource.States.TERMINATED:
            raise rf_exceptions.ValidationError({
                'resource': _('Resource is terminated.')
            })

        valid_components = set(offering.get_usage_components().keys())
        actual_components = {usage['type'] for usage in attrs['usages']}
        invalid_components = ', '.join(sorted(valid_components - actual_components))

        if invalid_components:
            raise rf_exceptions.ValidationError(_('These components are invalid: %s.') % invalid_components)

        return attrs

    def save(self):
        plan_period = self.validated_data['plan_period']
        resource = plan_period.resource
        components = resource.plan.offering.get_usage_components()
        date = datetime.date.today()

        for usage in self.validated_data['usages']:
            amount = usage['amount']
            description = usage.get('description', '')
            component = components[usage['type']]
            component.validate_amount(resource, amount, date)

            models.ComponentUsage.objects.update_or_create(
                resource=resource,
                component=component,
                plan_period=plan_period,
                defaults={'usage': amount, 'date': date, 'description': description},
            )


class OfferingFileSerializer(core_serializers.AugmentedSerializerMixin,
                             serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.OfferingFile
        fields = ('url', 'uuid', 'name', 'offering', 'created', 'file',)
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
            offering={'lookup_field': 'uuid', 'view_name': 'marketplace-offering-detail'},
        )


def validate_plan(plan):
    """"
    Ensure that maximum amount of resources with current plan is not reached yet.
    """
    if not plan.is_active:
        raise rf_exceptions.ValidationError({
            'plan': _('Plan is not available because limit has been reached.')
        })


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


def get_marketplace_offering_name(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.name
    except ObjectDoesNotExist:
        return


def get_marketplace_category_uuid(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.category.uuid
    except ObjectDoesNotExist:
        return


def get_marketplace_category_name(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.category.title
    except ObjectDoesNotExist:
        return


def get_marketplace_resource_uuid(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).uuid
    except ObjectDoesNotExist:
        return


def get_is_usage_based(serializer, scope):
    try:
        return models.Resource.objects.get(scope=scope).offering.is_usage_based
    except ObjectDoesNotExist:
        return


def add_marketplace_offering(sender, fields, **kwargs):
    fields['marketplace_offering_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_offering_uuid', get_marketplace_offering_uuid)

    fields['marketplace_offering_name'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_offering_name', get_marketplace_offering_name)

    fields['marketplace_category_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_category_uuid', get_marketplace_category_uuid)

    fields['marketplace_category_name'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_category_name', get_marketplace_category_name)

    fields['marketplace_resource_uuid'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_resource_uuid', get_marketplace_resource_uuid)

    fields['is_usage_based'] = serializers.SerializerMethodField()
    setattr(sender, 'get_is_usage_based', get_is_usage_based)


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
