import datetime
import logging
from functools import lru_cache
from typing import Dict

import jwt
from dateutil.parser import parse as parse_datetime
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework.reverse import reverse

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core.clean_html import clean_html
from waldur_core.core.fields import NaturalChoiceField
from waldur_core.core.serializers import GenericRelatedField
from waldur_core.media.serializers import (
    ProtectedFileField,
    ProtectedImageField,
    ProtectedMediaSerializerMixin,
)
from waldur_core.quotas.serializers import BasicQuotaSerializer
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure import utils as structure_utils
from waldur_core.structure.executors import ServiceSettingsCreateExecutor
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.serializers import ServiceSettingsSerializer
from waldur_mastermind.common import exceptions
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.serializers import validate_options
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.marketplace.permissions import (
    check_availability_of_auto_approving,
)
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.processors import CreateResourceProcessor
from waldur_mastermind.marketplace.utils import validate_attributes
from waldur_pid import models as pid_models

from . import log, models, permissions, plugins, tasks, utils

logger = logging.getLogger(__name__)
BillingTypes = models.OfferingComponent.BillingTypes


class MarketplaceProtectedMediaSerializerMixin(serializers.ModelSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not settings.WALDUR_MARKETPLACE['ANONYMOUS_USER_CAN_VIEW_OFFERINGS']:
            self.serializer_field_mapping = (
                ProtectedMediaSerializerMixin.serializer_field_mapping
            )


class ServiceProviderSerializer(
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.ServiceProvider
        fields = (
            'url',
            'uuid',
            'created',
            'description',
            'enable_notifications',
            'customer',
            'customer_name',
            'customer_uuid',
            'customer_image',
            'customer_abbreviation',
            'customer_native_name',
            'customer_country',
            'image',
            'division',
            'description',
        )
        related_paths = {'customer': ('uuid', 'name', 'native_name', 'abbreviation')}
        protected_fields = ('customer',)
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-service-provider-detail',
            },
            'customer': {'lookup_field': 'uuid'},
        }

    customer_image = ProtectedImageField(source='customer.image', read_only=True)
    customer_country = serializers.CharField(source='customer.country', read_only=True)
    division = serializers.CharField(source='customer.division', read_only=True)

    def get_fields(self):
        fields = super(ServiceProviderSerializer, self).get_fields()
        if self.context['request'].user.is_anonymous:
            del fields['enable_notifications']
        if settings.WALDUR_MARKETPLACE['ANONYMOUS_USER_CAN_VIEW_OFFERINGS']:
            fields['customer_image'] = serializers.ImageField(
                source='customer.image', read_only=True
            )
        return fields

    def validate(self, attrs):
        if not self.instance:
            permissions.can_register_service_provider(
                self.context['request'], attrs['customer']
            )
        return attrs


class SetOfferingsUsernameSerializer(serializers.Serializer):
    user_uuid = serializers.UUIDField()
    username = serializers.CharField()


class NestedAttributeOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.AttributeOption
        fields = ('key', 'title')


class NestedAttributeSerializer(serializers.ModelSerializer):
    options = NestedAttributeOptionSerializer(many=True)

    class Meta:
        model = models.Attribute
        fields = ('key', 'title', 'type', 'options', 'required', 'default')


class NestedSectionSerializer(serializers.ModelSerializer):
    attributes = NestedAttributeSerializer(many=True, read_only=True)

    class Meta:
        model = models.Section
        fields = ('key', 'title', 'attributes', 'is_standalone')


class NestedColumnSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.CategoryColumn
        fields = ('index', 'title', 'attribute', 'widget')


class CategoryComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.CategoryComponent
        fields = ('type', 'name', 'description', 'measured_unit')


class CategoryHelpArticleSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.CategoryHelpArticle
        fields = ('title', 'url')


class CategorySerializer(
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    offering_count = serializers.SerializerMethodField()
    available_offerings_count = serializers.ReadOnlyField()
    sections = NestedSectionSerializer(many=True, read_only=True)
    columns = NestedColumnSerializer(many=True, read_only=True)
    components = CategoryComponentSerializer(many=True, read_only=True)
    articles = CategoryHelpArticleSerializer(many=True, read_only=True)

    @staticmethod
    def eager_load(queryset, request):
        return queryset.distinct().prefetch_related('sections', 'sections__attributes')

    def get_offering_count(self, category):
        request = self.context['request']
        customer_uuid = request.GET.get('customer_uuid')
        shared = request.GET.get('shared')

        try:
            shared = forms.NullBooleanField().to_python(shared)
        except rf_exceptions.ValidationError:
            shared = None

        # counting available offerings for resource order.
        offerings = (
            models.Offering.objects.filter(category=category)
            .filter_by_ordering_availability_for_user(request.user)
            .order_by()
        )

        allowed_customer_uuid = request.query_params.get('allowed_customer_uuid')
        if allowed_customer_uuid and core_utils.is_uuid_like(allowed_customer_uuid):
            offerings = offerings.filter_for_customer(allowed_customer_uuid)

        project_uuid = request.query_params.get('project_uuid')
        if project_uuid and core_utils.is_uuid_like(project_uuid):
            offerings = offerings.filter_for_project(project_uuid)

        offering_name = request.query_params.get('offering_name')
        if offering_name:
            offerings = offerings.filter(name__icontains=offering_name)

        if customer_uuid:
            offerings = offerings.filter(customer__uuid=customer_uuid)

        if shared is not None:
            offerings = offerings.filter(shared=shared)

        return offerings.count()

    class Meta:
        model = models.Category
        fields = (
            'url',
            'uuid',
            'title',
            'description',
            'icon',
            'offering_count',
            'available_offerings_count',
            'sections',
            'columns',
            'components',
            'articles',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'marketplace-category-detail'},
        }


PriceSerializer = serializers.DecimalField(
    min_value=0,
    max_digits=common_mixins.PRICE_MAX_DIGITS,
    decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
)


class BasePlanSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    prices = serializers.DictField(
        child=PriceSerializer, write_only=True, required=False
    )
    quotas = serializers.DictField(
        child=serializers.IntegerField(min_value=0), write_only=True, required=False
    )
    divisions = structure_serializers.DivisionSerializer(many=True, read_only=True)

    class Meta:
        model = models.Plan
        fields = (
            'url',
            'uuid',
            'name',
            'description',
            'article_code',
            'prices',
            'quotas',
            'max_amount',
            'archived',
            'is_active',
            'unit_price',
            'unit',
            'init_price',
            'switch_price',
            'backend_id',
            'divisions',
        )
        read_ony_fields = ('unit_price', 'archived')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_fields(self):
        fields = super().get_fields()
        method = self.context['view'].request.method
        if method == 'GET':
            fields['prices'] = serializers.SerializerMethodField()
            fields['quotas'] = serializers.SerializerMethodField()
            fields['plan_type'] = serializers.SerializerMethodField()
            fields['minimal_price'] = serializers.SerializerMethodField()
        return fields

    def get_prices(self, plan):
        return {item.component.type: item.price for item in plan.components.all()}

    def get_quotas(self, plan):
        return {item.component.type: item.amount for item in plan.components.all()}

    def get_plan_type(self, plan):
        plan_type = None
        components_types = set()

        for plan_component in plan.components.all():
            offering_component = plan_component.component

            if plan_component.price:
                components_types.add(offering_component.billing_type)

        if len(components_types) == 1:
            if models.OfferingComponent.BillingTypes.USAGE in components_types:
                plan_type = 'usage-based'
            if models.OfferingComponent.BillingTypes.FIXED in components_types:
                plan_type = 'fixed'
            if models.OfferingComponent.BillingTypes.ONE_TIME in components_types:
                plan_type = 'one-time'
            if models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH in components_types:
                plan_type = 'on-plan-switch'
            if models.OfferingComponent.BillingTypes.LIMIT in components_types:
                plan_type = 'limit'
        elif len(components_types) > 1:
            plan_type = 'mixed'

        return plan_type

    def get_minimal_price(self, plan):
        price = 0

        for plan_component in plan.components.all():
            offering_component = plan_component.component

            if plan_component.price:
                if (
                    offering_component.billing_type
                    == models.OfferingComponent.BillingTypes.LIMIT
                ):
                    price += plan_component.price
                elif (
                    offering_component.billing_type
                    == models.OfferingComponent.BillingTypes.FIXED
                ):
                    price += plan_component.price * (plan_component.amount or 1)
                elif (
                    offering_component.billing_type
                    == models.OfferingComponent.BillingTypes.ONE_TIME
                ):
                    price += plan_component.price

        return price

    def validate_description(self, value):
        return clean_html(value)


class BasePublicPlanSerializer(BasePlanSerializer):
    """Serializer to display the public plan without offering info."""

    class Meta(BasePlanSerializer.Meta):
        view_name = 'marketplace-public-plan-detail'


class BaseProviderPlanSerializer(BasePlanSerializer):
    """Serializer to display the provider's plan without offering info."""

    class Meta(BasePlanSerializer.Meta):
        view_name = 'marketplace-plan-detail'


class ProviderPlanDetailsSerializer(BaseProviderPlanSerializer):
    """Serializer to display the provider's plan in the REST API."""

    class Meta(BaseProviderPlanSerializer.Meta):
        fields = BaseProviderPlanSerializer.Meta.fields + ('offering',)
        protected_fields = ('offering',)
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
            },
            'offering': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-provider-offering-detail',
            },
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(
                self.context['request'], None, attrs['offering'].customer
            )

        return attrs


class PublicPlanDetailsSerializer(BasePublicPlanSerializer):
    """Serializer to display the public plan in the REST API."""

    class Meta(BasePublicPlanSerializer.Meta):
        fields = BasePublicPlanSerializer.Meta.fields + ('offering',)
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
            },
            'offering': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-public-offering-detail',
            },
        }


class PlanUsageRequestSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(required=False)
    customer_provider_uuid = serializers.UUIDField(required=False)
    o = serializers.ChoiceField(
        choices=(
            'usage',
            'limit',
            'remaining',
            '-usage',
            '-limit',
            '-remaining',
        ),
        required=False,
    )


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


class NestedScreenshotSerializer(
    MarketplaceProtectedMediaSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = models.Screenshot
        fields = ('name', 'uuid', 'description', 'image', 'thumbnail', 'created')


class NestedOfferingFileSerializer(
    MarketplaceProtectedMediaSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = models.OfferingFile
        fields = (
            'name',
            'created',
            'file',
        )


class ScreenshotSerializer(
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.Screenshot
        fields = (
            'url',
            'uuid',
            'name',
            'created',
            'description',
            'image',
            'thumbnail',
            'offering',
        )
        protected_fields = ('offering', 'image')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'offering': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-provider-offering-detail',
            },
        }

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(
                self.context['request'], None, attrs['offering'].customer
            )
        return attrs


FIELD_TYPES = (
    'boolean',
    'integer',
    'money',
    'string',
    'text',
    'html_text',
    'select_string',
    'select_string_multi',
    'select_openstack_tenant',
    'select_multiple_openstack_tenants',
    'select_openstack_instance',
    'select_multiple_openstack_instances',
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
    factor = serializers.SerializerMethodField()

    class Meta:
        model = models.OfferingComponent
        fields = (
            'billing_type',
            'type',
            'name',
            'description',
            'measured_unit',
            'limit_period',
            'limit_amount',
            'article_code',
            'max_value',
            'min_value',
            'max_available_limit',
            'is_boolean',
            'default_limit',
            'factor',
            'is_builtin',
        )
        extra_kwargs = {
            'billing_type': {'required': True},
        }

    def validate(self, attrs):
        if attrs.get('is_boolean'):
            attrs['min_value'] = 0
            attrs['max_value'] = 1
            attrs['limit_period'] = ''
            attrs['limit_amount'] = None
        return attrs

    def get_factor(self, offering_component):
        builtin_components = plugins.manager.get_components(
            offering_component.offering.type
        )
        for c in builtin_components:
            if c.type == offering_component.type:
                return c.factor


class ExportImportOfferingComponentSerializer(OfferingComponentSerializer):
    offering_id = serializers.IntegerField(write_only=True, required=False)

    class Meta(OfferingComponentSerializer.Meta):
        fields = OfferingComponentSerializer.Meta.fields + ('offering_id',)


class ExportImportPlanComponentSerializer(serializers.ModelSerializer):
    component = ExportImportOfferingComponentSerializer(required=False)
    component_id = serializers.IntegerField(write_only=True, required=False)
    plan_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = models.PlanComponent
        fields = (
            'amount',
            'price',
            'component',
            'component_id',
            'plan_id',
        )


class ExportImportPlanSerializer(serializers.ModelSerializer):
    """Serializer for export and import of plan from/to an exported offering.
    This serializer differs from PlanDetailsSerializer in methods and fields."""

    components = ExportImportPlanComponentSerializer(many=True)
    offering_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = models.Plan
        fields = (
            'name',
            'description',
            'article_code',
            'max_amount',
            'archived',
            'is_active',
            'unit_price',
            'unit',
            'init_price',
            'switch_price',
            'components',
            'offering_id',
        )

    def save(self, **kwargs):
        validated_data = self.validated_data
        components = validated_data.pop('components', [])
        plan = super(ExportImportPlanSerializer, self).save(**kwargs)

        offering_components = []

        for component in components:
            serialized_offering_component = component.get('component')

            if serialized_offering_component:
                offering_component = plan.offering.components.get(
                    type=serialized_offering_component['type']
                )
                offering_components.append(offering_component)

        plan.components.exclude(component__in=offering_components).delete()

        for component in components:
            component['plan_id'] = plan.id
            serialized_offering_component = component.pop('component')

            if serialized_offering_component:
                offering_component = plan.offering.components.get(
                    type=serialized_offering_component['type']
                )
                component['component_id'] = offering_component.id
                offering_components.append(offering_component)

                if plan.components.filter(
                    component_id=component['component_id']
                ).exists():
                    existed_component = plan.components.get(
                        component_id=component['component_id']
                    )
                    component_serializer = ExportImportPlanComponentSerializer(
                        existed_component, data=component
                    )
                else:
                    component_serializer = ExportImportPlanComponentSerializer(
                        data=component
                    )
            else:
                component_serializer = ExportImportPlanComponentSerializer(
                    data=component
                )

            component_serializer.is_valid(raise_exception=True)
            component_serializer.save()

        return plan


class ExportImportOfferingSerializer(serializers.ModelSerializer):
    category_id = serializers.IntegerField(write_only=True, required=False)
    customer_id = serializers.IntegerField(write_only=True, required=False)
    components = ExportImportOfferingComponentSerializer(many=True)
    plans = ExportImportPlanSerializer(many=True)

    class Meta:
        model = models.Offering
        fields = (
            'name',
            'description',
            'full_description',
            'terms_of_service',
            'rating',
            'attributes',
            'options',
            'components',
            'plugin_options',
            'secret_options',
            'state',
            'native_name',
            'native_description',
            'vendor_details',
            'type',
            'shared',
            'billable',
            'category_id',
            'customer_id',
            'plans',
            'latitude',
            'longitude',
        )

    def save(self, **kwargs):
        validated_data = self.validated_data
        components = validated_data.pop('components', [])
        plans = validated_data.pop('plans', [])
        offering = super(ExportImportOfferingSerializer, self).save(**kwargs)

        component_types = []

        for component in components:
            component['offering_id'] = offering.id
            component_types.append(component['type'])

            if offering.components.filter(type=component['type']).exists():
                existed_component = offering.components.get(type=component['type'])
                component_serializer = ExportImportOfferingComponentSerializer(
                    existed_component, data=component
                )
            else:
                component_serializer = ExportImportOfferingComponentSerializer(
                    data=component
                )

            component_serializer.is_valid(raise_exception=True)
            component_serializer.save()

        offering.components.exclude(type__in=component_types).delete()

        plan_names = []

        for plan in plans:
            plan['offering_id'] = offering.id
            plan_names.append(plan['name'])

            if offering.plans.filter(name=plan['name']).exists():
                existed_plan = offering.plans.get(name=plan['name'])
                plan_serializer = ExportImportPlanSerializer(existed_plan, data=plan)
            else:
                plan_serializer = ExportImportPlanSerializer(data=plan)

            plan_serializer.is_valid(raise_exception=True)
            plan_serializer.save()

        offering.plans.exclude(name__in=plan_names).delete()

        return offering


class PlanComponentSerializer(serializers.ModelSerializer):
    offering_name = serializers.ReadOnlyField(source='plan.offering.name')
    plan_name = serializers.ReadOnlyField(source='plan.name')
    plan_unit = serializers.ReadOnlyField(source='plan.unit')
    component_name = serializers.ReadOnlyField(source='component.name')
    measured_unit = serializers.ReadOnlyField(source='component.measured_unit')
    billing_type = serializers.ReadOnlyField(source='component.billing_type')

    class Meta:
        model = models.PlanComponent
        fields = (
            'offering_name',
            'plan_name',
            'plan_unit',
            'component_name',
            'measured_unit',
            'billing_type',
            'amount',
            'price',
        )


class NestedCustomerSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = structure_models.Customer
        fields = ('uuid', 'name', 'url')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class OfferingDetailsSerializer(
    core_serializers.RestrictedSerializerMixin,
    structure_serializers.CountrySerializerMixin,
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    attributes = serializers.JSONField(required=False)
    options = serializers.JSONField(
        required=False, default={'options': {}, 'order': []}
    )
    secret_options = serializers.JSONField(required=False)
    components = OfferingComponentSerializer(required=False, many=True)
    order_item_count = serializers.SerializerMethodField()
    plans = BaseProviderPlanSerializer(many=True, required=False)
    screenshots = NestedScreenshotSerializer(many=True, read_only=True)
    state = serializers.ReadOnlyField(source='get_state_display')
    scope = GenericRelatedField(read_only=True)
    scope_uuid = serializers.ReadOnlyField(source='scope.uuid')
    files = NestedOfferingFileSerializer(many=True, read_only=True)
    quotas = serializers.SerializerMethodField()
    divisions = structure_serializers.DivisionSerializer(many=True, read_only=True)
    total_customers = serializers.ReadOnlyField()
    total_cost = serializers.ReadOnlyField()
    total_cost_estimated = serializers.ReadOnlyField()

    class Meta:
        model = models.Offering
        fields = (
            'url',
            'uuid',
            'created',
            'name',
            'description',
            'full_description',
            'terms_of_service',
            'terms_of_service_link',
            'privacy_policy_link',
            'access_url',
            'customer',
            'customer_uuid',
            'customer_name',
            'category',
            'category_uuid',
            'category_title',
            'rating',
            'attributes',
            'options',
            'components',
            'plugin_options',
            'secret_options',
            'state',
            'native_name',
            'native_description',
            'vendor_details',
            'thumbnail',
            'order_item_count',
            'plans',
            'screenshots',
            'type',
            'shared',
            'billable',
            'scope',
            'scope_uuid',
            'files',
            'quotas',
            'paused_reason',
            'datacite_doi',
            'citation_count',
            'latitude',
            'longitude',
            'country',
            'backend_id',
            'divisions',
            'image',
            'total_customers',
            'total_cost',
            'total_cost_estimated',
        )
        related_paths = {
            'customer': ('uuid', 'name'),
            'category': ('uuid', 'title'),
        }
        protected_fields = ('customer', 'type')
        read_only_fields = (
            'state',
            'paused_reason',
            'citation_count',
        )
        extra_kwargs = {
            'url': {
                'lookup_field': 'uuid',
            },
            'customer': {'lookup_field': 'uuid', 'view_name': 'customer-detail'},
            'category': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-category-detail',
            },
        }
        view_name = 'marketplace-provider-offering-detail'

    def get_fields(self):
        fields = super().get_fields()
        if (
            self.instance
            and not self.can_see_secret_options()
            and 'secret_options' in fields
        ):
            del fields['secret_options']
        method = self.context['view'].request.method
        if method == 'GET':
            if 'components' in fields:
                fields['components'] = serializers.SerializerMethodField(
                    'get_components'
                )
            if 'plans' in fields:
                fields['plans'] = serializers.SerializerMethodField(
                    'get_filtered_plans'
                )
            if 'attributes' in fields:
                fields['attributes'] = serializers.SerializerMethodField(
                    'get_attributes'
                )

        user = self.context['view'].request.user
        if not user.is_authenticated:
            fields.pop('scope', None)
            fields.pop('scope_uuid', None)

        return fields

    def can_see_secret_options(self):
        user = None
        try:
            request = self.context['request']
            user = request.user
            if user.is_anonymous:
                return

        except (KeyError, AttributeError):
            pass

        offering = None
        if isinstance(self.instance, list):
            if len(self.instance) == 1:
                offering = self.instance[0]
        else:
            offering = self.instance

        return (
            offering
            and user
            and structure_permissions._has_owner_access(user, offering.customer)
        )

    def get_order_item_count(self, offering):
        try:
            return offering.quotas.get(name='order_item_count').usage
        except ObjectDoesNotExist:
            return 0

    def get_quotas(self, offering):
        if getattr(offering, 'scope', None) and hasattr(offering.scope, 'quotas'):
            return BasicQuotaSerializer(
                offering.scope.quotas, many=True, context=self.context
            ).data

    def get_components(self, offering):
        qs = (offering.parent or offering).components
        func = manager.get_components_filter(offering.type)
        if func:
            qs = func(offering, qs)
        return OfferingComponentSerializer(qs, many=True, context=self.context).data

    def get_filtered_plans(self, offering):
        qs = (offering.parent or offering).plans.all()
        customer_uuid = self.context['request'].GET.get('allowed_customer_uuid')
        user = self.context['request'].user

        if user.is_anonymous:
            qs = qs.filter(divisions__isnull=True)
        elif user.is_staff or user.is_support:
            pass
        elif customer_uuid:
            qs = qs.filter(
                Q(divisions__isnull=True) | Q(divisions__in=user.divisions)
            ).filter_for_customer(customer_uuid)
        else:
            qs = qs.filter(Q(divisions__isnull=True) | Q(divisions__in=user.divisions))

        return BaseProviderPlanSerializer(qs, many=True, context=self.context).data

    def get_attributes(self, offering):
        func = manager.get_change_attributes_for_view(offering.type)

        if func:
            return func(offering.attributes)

        return offering.attributes


class ProviderOfferingDetailsSerializer(OfferingDetailsSerializer):
    class Meta(OfferingDetailsSerializer.Meta):
        view_name = 'marketplace-provider-offering-detail'

    def get_filtered_plans(self, offering):
        qs = (offering.parent or offering).plans.all()
        customer_uuid = self.context['request'].GET.get('allowed_customer_uuid')
        user = self.context['request'].user

        if user.is_anonymous:
            qs = qs.filter(divisions__isnull=True)
        elif user.is_staff or user.is_support:
            pass
        elif customer_uuid:
            qs = qs.filter(
                Q(divisions__isnull=True) | Q(divisions__in=user.divisions)
            ).filter_for_customer(customer_uuid)
        else:
            qs = qs.filter(Q(divisions__isnull=True) | Q(divisions__in=user.divisions))

        return BaseProviderPlanSerializer(qs, many=True, context=self.context).data


class PublicOfferingDetailsSerializer(OfferingDetailsSerializer):
    class Meta(OfferingDetailsSerializer.Meta):
        view_name = 'marketplace-public-offering-detail'

    def get_filtered_plans(self, offering):
        qs = (offering.parent or offering).plans.all()
        customer_uuid = self.context['request'].GET.get('allowed_customer_uuid')
        user = self.context['request'].user

        if user.is_anonymous:
            qs = qs.filter(divisions__isnull=True)
        elif user.is_staff or user.is_support:
            pass
        elif customer_uuid:
            qs = qs.filter(
                Q(divisions__isnull=True) | Q(divisions__in=user.divisions)
            ).filter_for_customer(customer_uuid)
        else:
            qs = qs.filter(Q(divisions__isnull=True) | Q(divisions__in=user.divisions))

        return BasePublicPlanSerializer(qs, many=True, context=self.context).data


class OfferingComponentLimitSerializer(serializers.Serializer):
    min = serializers.IntegerField(min_value=0)
    max = serializers.IntegerField(min_value=0)
    max_available_limit = serializers.IntegerField(min_value=0)


class OfferingModifySerializer(ProviderOfferingDetailsSerializer):
    class Meta(ProviderOfferingDetailsSerializer.Meta):
        model = models.Offering
        fields = ProviderOfferingDetailsSerializer.Meta.fields + ('limits',)

    limits = serializers.DictField(
        child=OfferingComponentLimitSerializer(), write_only=True, required=False
    )

    def validate(self, attrs):
        if not self.instance:
            structure_permissions.is_owner(
                self.context['request'], None, attrs['customer']
            )

        self._validate_attributes(attrs)
        self._validate_plans(attrs)

        return attrs

    def validate_type(self, offering_type):
        if offering_type not in plugins.manager.backends.keys():
            raise rf_exceptions.ValidationError(_('Invalid value.'))
        return offering_type

    def validate_terms_of_service(self, value):
        return clean_html(value.strip())

    def validate_description(self, value):
        return clean_html(value.strip())

    def validate_full_description(self, value):
        return clean_html(value.strip())

    def validate_vendor_details(self, value):
        return clean_html(value.strip())

    def _validate_attributes(self, attrs):
        category = attrs.get('category')
        if category is None and self.instance:
            category = self.instance.category

        attributes = attrs.get('attributes')
        if attributes is not None and not isinstance(attributes, dict):
            raise rf_exceptions.ValidationError(
                {
                    'attributes': _('Dictionary is expected.'),
                }
            )

        if attributes is None and self.instance:
            return

        if attributes is None:
            attributes = dict()

        validate_attributes(attributes, category)

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
            if {c.get('type') for c in attrs.get('components')} - {
                c.type for c in builtin_components
            }:
                raise serializers.ValidationError(
                    {'components': _('Extra components are not allowed.')}
                )
            valid_types = {component.type for component in builtin_components}

        elif builtin_components:
            valid_types = {component.type for component in builtin_components}
            if self.instance:
                valid_types.update(
                    set(self.instance.components.values_list('type', flat=True))
                )
            fixed_types = {
                component.type
                for component in plugins.manager.get_components(offering_type)
                if component.billing_type == BillingTypes.FIXED
            }
            if self.instance:
                fixed_types.update(
                    set(
                        self.instance.components.filter(
                            billing_type=BillingTypes.FIXED
                        ).values_list('type', flat=True)
                    )
                )

        elif custom_components:
            valid_types = {component['type'] for component in custom_components}
            fixed_types = {
                component['type']
                for component in custom_components
                if component['billing_type'] == BillingTypes.FIXED
            }

        for plan in attrs.get('plans', []):
            plan_name = plan.get('name')

            prices = plan.get('prices', {})
            invalid_components = ', '.join(sorted(set(prices.keys()) - valid_types))
            if invalid_components:
                raise serializers.ValidationError(
                    {
                        'plans': _('Invalid price components %s in plan "%s".')
                        % (invalid_components, plan_name)
                    }
                )

            quotas = plan.get('quotas', {})
            invalid_components = ', '.join(sorted(set(quotas.keys()) - fixed_types))
            if invalid_components:
                raise serializers.ValidationError(
                    {
                        'plans': _('Invalid quota components %s in plan "%s".')
                        % (invalid_components, plan_name),
                    }
                )

            plan['unit_price'] = sum(
                prices.get(component, 0) * quotas.get(component, 0)
                for component in fixed_types
            )

    def _create_plan(self, offering, plan_data, components):
        quotas = plan_data.pop('quotas', {})
        prices = plan_data.pop('prices', {})
        plan = models.Plan.objects.create(offering=offering, **plan_data)

        for name, component in components.items():
            models.PlanComponent.objects.create(
                plan=plan,
                component=component,
                amount=quotas.get(name) or 0,
                price=prices.get(name) or 0,
            )

    def _create_plans(self, offering, plans):
        components = {
            component.type: component for component in offering.components.all()
        }
        for plan_data in plans:
            self._create_plan(offering, plan_data, components)

    def _update_limits(self, offering, limits):
        for key, values in limits.items():
            min_value = values.get('min_value') or values.get('min')
            max_value = values.get('max_value') or values.get('max')
            max_available_limit = values.get('max_available_limit')

            models.OfferingComponent.objects.filter(offering=offering, type=key).update(
                min_value=min_value,
                max_value=max_value,
                max_available_limit=max_available_limit,
                article_code=values.get('article_code', ''),
            )


class OfferingCreateSerializer(OfferingModifySerializer):
    class Meta(OfferingModifySerializer.Meta):
        fields = OfferingModifySerializer.Meta.fields + ('service_attributes',)

    service_attributes = serializers.JSONField(required=False, write_only=True)

    def validate_plans(self, plans):
        if len(plans) < 1:
            raise serializers.ValidationError(
                {'plans': _('At least one plan should be specified.')}
            )
        return plans

    @transaction.atomic
    def create(self, validated_data):
        plans = validated_data.pop('plans', [])

        limits = validated_data.pop('limits', {})

        if not limits:
            custom_components = []
            limits = {}

            for component in validated_data.pop('components', []):
                if component['type'] in [
                    c.type
                    for c in plugins.manager.get_components(validated_data['type'])
                ]:
                    limits[component['type']] = component
                else:
                    custom_components.append(component)
        else:
            custom_components = validated_data.pop('components', [])

        validated_data = self._create_service(validated_data)

        offering = super(OfferingCreateSerializer, self).create(validated_data)
        utils.create_offering_components(offering, custom_components)
        if limits:
            self._update_limits(offering, limits)
        self._create_plans(offering, plans)

        return offering

    def _create_service(self, validated_data):
        """
        Marketplace offering model does not accept service_attributes field as is,
        therefore we should remove it from validated_data and create service settings object.
        Then we need to specify created object and offering's scope.
        """
        offering_type = validated_data.get('type')
        service_type = plugins.manager.get_service_type(offering_type)

        name = validated_data['name']
        service_attributes = validated_data.pop('service_attributes', {})

        if not service_type:
            return validated_data

        if not service_attributes:
            raise ValidationError({'service_attributes': _('This field is required.')})
        payload = dict(
            name=name,
            # It is expected that customer URL is passed to the service settings serializer
            customer=self.initial_data['customer'],
            type=service_type,
            options=service_attributes,
        )
        serializer = ServiceSettingsSerializer(data=payload, context=self.context)
        serializer.is_valid(raise_exception=True)
        service_settings = serializer.save()
        # Usually we don't allow users to create new shared service settings via REST API.
        # That's shared flag is marked as read-only in service settings serializer.
        # But shared offering should be created with shared service settings.
        # That's why we set it to shared only after service settings object is created.
        if validated_data.get('shared'):
            service_settings.shared = True
            service_settings.save()

        # XXX: dirty hack to trigger pulling of services after saving
        transaction.on_commit(
            lambda: ServiceSettingsCreateExecutor.execute(service_settings)
        )
        validated_data['scope'] = service_settings
        return validated_data


class OfferingPauseSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Offering
        fields = ['paused_reason']


class PlanUpdateSerializer(BaseProviderPlanSerializer):
    class Meta(BaseProviderPlanSerializer.Meta):
        extra_kwargs = {
            'uuid': {'read_only': False},
        }


class OfferingUpdateSerializer(OfferingModifySerializer):

    plans = PlanUpdateSerializer(many=True, required=False, write_only=True)

    def _update_components(self, instance, components):
        resources_exist = models.Resource.objects.filter(offering=instance).exists()

        old_components = {
            component.type: component for component in instance.components.all()
        }

        new_components = {
            component['type']: models.OfferingComponent(offering=instance, **component)
            for component in components
        }

        removed_components = set(old_components.keys()) - set(new_components.keys())
        added_components = set(new_components.keys()) - set(old_components.keys())
        updated_components = set(new_components.keys()) & set(old_components.keys())

        builtin_components = plugins.manager.get_components(self.instance.type)
        valid_types = {component.type for component in builtin_components}

        if removed_components & valid_types:
            raise serializers.ValidationError(
                {
                    'components': _(
                        'These components cannot be removed because they are builtin: %s'
                    )
                    % ', '.join(removed_components & valid_types)
                }
            )

        if removed_components:
            if resources_exist:
                raise serializers.ValidationError(
                    {
                        'components': _(
                            'These components cannot be removed because they are already used: %s'
                        )
                        % ', '.join(removed_components)
                    }
                )
            else:
                models.OfferingComponent.objects.filter(
                    type__in=removed_components
                ).delete()

        for key in added_components:
            new_components[key].save()

        if updated_components & valid_types:
            COMPONENT_KEYS = ('article_code',)
        else:
            COMPONENT_KEYS = (
                'name',
                'description',
                'billing_type',
                'measured_unit',
                'limit_period',
                'limit_amount',
                'article_code',
                'is_boolean',
                'default_limit',
                'min_value',
                'max_value',
                'max_available_limit',
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

    def _update_plan_details(self, old_plan, new_plan, offering):
        plan_fields_that_cannot_be_edited = (
            plugins.manager.get_plan_fields_that_cannot_be_edited(offering.type)
        )
        PLAN_FIELDS = {
            'name',
            'description',
            'unit',
            'max_amount',
            'article_code',
        }.difference(set(plan_fields_that_cannot_be_edited))

        for key in PLAN_FIELDS:
            if key in new_plan:
                setattr(old_plan, key, new_plan.get(key))
        old_plan.save()

    def _update_plans(self, offering, new_plans):
        can_manage_plans = plugins.manager.can_manage_plans(offering.type)

        old_plans = offering.plans.all()
        old_ids = set(old_plans.values_list('uuid', flat=True))

        new_map = {plan['uuid']: plan for plan in new_plans if 'uuid' in plan}
        added_plans = [plan for plan in new_plans if 'uuid' not in plan]

        removed_ids = set(old_ids) - set(new_map.keys())
        updated_ids = set(new_map.keys()) & set(old_ids)

        removed_plans = models.Plan.objects.filter(uuid__in=removed_ids).exclude(
            archived=True
        )
        updated_plans = {
            plan.uuid: plan for plan in models.Plan.objects.filter(uuid__in=updated_ids)
        }

        for plan_uuid, old_plan in updated_plans.items():
            new_plan = new_map[plan_uuid]
            self._update_plan_details(old_plan, new_plan, offering)
            if can_manage_plans:
                self._update_plan_components(old_plan, new_plan)
            self._update_quotas(old_plan, new_plan)

        if can_manage_plans:
            if added_plans:
                self._create_plans(offering, added_plans)

            for plan in removed_plans:
                plan.archived = True
                plan.save()

    @transaction.atomic
    def update(self, instance, validated_data):
        """
        Components and plans are specified using nested list serializers with many=True.
        These serializers return empty list even if value is not provided explicitly.
        See also: https://github.com/encode/django-rest-framework/issues/3434
        Consider the case when offering's thumbnail is uploaded, but plans and components are not specified.
        It leads to tricky bug when all components are removed and plans are marked as archived.
        In order to distinguish between case when user asks to remove all plans and
        case when user wants to update only one attribute these we need to check not only
        validated data, but also initial data.
        """
        if 'components' in validated_data:
            components = validated_data.pop('components', [])
            if 'components' in self.initial_data:
                if plugins.manager.can_manage_offering_components(instance.type):
                    self._update_components(instance, components)
        if 'plans' in validated_data:
            new_plans = validated_data.pop('plans', [])
            if 'plans' in self.initial_data:
                self._update_plans(instance, new_plans)
        limits = validated_data.pop('limits', {})
        if limits:
            self._update_limits(instance, limits)
        offering = super(OfferingUpdateSerializer, self).update(
            instance, validated_data
        )
        return offering


class OfferingLocationUpdateSerializer(serializers.ModelSerializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()

    class Meta:
        model = models.Offering
        fields = (
            'latitude',
            'longitude',
        )


class OfferingDescriptionUpdateSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.Offering
        fields = ('category',)

        related_paths = {
            'category': ('uuid', 'title'),
        }

        extra_kwargs = {
            'category': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-category-detail',
            },
        }


class OfferingOverviewUpdateSerializer(
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    def validate_terms_of_service(self, value):
        return clean_html(value.strip())

    def validate_description(self, value):
        return clean_html(value.strip())

    def validate_full_description(self, value):
        return clean_html(value.strip())

    class Meta:
        model = models.Offering
        fields = (
            'name',
            'description',
            'full_description',
            'terms_of_service',
            'terms_of_service_link',
            'privacy_policy_link',
            'access_url',
        )


class OfferingOptionsUpdateSerializer(serializers.ModelSerializer):
    def validate_options(self, options):
        serializer = OfferingOptionsSerializer(data=options)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    class Meta:
        model = models.Offering
        fields = ('options',)


class OfferingSecretOptionsUpdateSerializer(serializers.ModelSerializer):
    def validate_options(self, options):
        serializer = OfferingOptionsSerializer(data=options)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    class Meta:
        model = models.Offering
        fields = ('secret_options',)


class OfferingPermissionSerializer(
    structure_serializers.PermissionFieldFilteringMixin,
    structure_serializers.BasePermissionSerializer,
):
    offering_name = serializers.ReadOnlyField(source='offering.name')

    class Meta(structure_serializers.BasePermissionSerializer.Meta):
        model = models.OfferingPermission
        fields = (
            'url',
            'pk',
            'created',
            'expiration_time',
            'created_by',
            'offering',
            'offering_uuid',
            'offering_name',
        ) + structure_serializers.BasePermissionSerializer.Meta.fields
        related_paths = dict(
            offering=('name', 'uuid'),
            **structure_serializers.BasePermissionSerializer.Meta.related_paths,
        )
        protected_fields = ('offering', 'user', 'created_by', 'created')
        extra_kwargs = {
            'user': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'queryset': get_user_model().objects.all(),
            },
            'created_by': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'read_only': True,
            },
            'offering': {
                'view_name': 'marketplace-provider-offering-detail',
                'lookup_field': 'uuid',
                'queryset': models.Offering.objects.all(),
            },
        }

    def validate(self, data):
        if not self.instance:
            offering = data['offering']
            user = data['user']

            if offering.has_user(user):
                raise serializers.ValidationError(
                    _('The fields offering and user must make a unique set.')
                )

        return data

    def create(self, validated_data):
        offering = validated_data['offering']
        user = validated_data['user']
        expiration_time = validated_data.get('expiration_time')

        created_by = self.context['request'].user
        permission, _ = offering.add_user(
            user=user, created_by=created_by, expiration_time=expiration_time
        )

        return permission

    def validate_expiration_time(self, value):
        if value is not None and value < timezone.now():
            raise serializers.ValidationError(
                _('Expiration time should be greater than current time.')
            )
        return value

    def get_filtered_field_names(self):
        return ('offering',)


class OfferingPermissionLogSerializer(OfferingPermissionSerializer):
    class Meta(OfferingPermissionSerializer.Meta):
        view_name = 'marketplace-offering-permission-log-detail'


class ComponentQuotaSerializer(serializers.ModelSerializer):
    type = serializers.ReadOnlyField(source='component.type')

    class Meta:
        model = models.ComponentQuota
        fields = ('type', 'limit', 'usage')


class BaseItemSerializer(
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        fields = (
            'offering',
            'offering_name',
            'offering_uuid',
            'offering_description',
            'offering_image',
            'offering_thumbnail',
            'offering_type',
            'offering_terms_of_service',
            'offering_shared',
            'offering_billable',
            'provider_name',
            'provider_uuid',
            'category_title',
            'category_uuid',
            'category_icon',
            'plan',
            'plan_unit',
            'plan_name',
            'plan_uuid',
            'plan_description',
            'attributes',
            'limits',
            'uuid',
            'created',
            'modified',
        )
        related_paths = {
            'offering': (
                'name',
                'uuid',
                'description',
                'image',
                'thumbnail',
                'type',
                'terms_of_service',
                'shared',
                'billable',
            ),
            'plan': ('unit', 'uuid', 'name', 'description'),
        }
        protected_fields = ('offering',)
        extra_kwargs = {
            'offering': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-public-offering-detail',
            },
            'plan': {
                'lookup_field': 'uuid',
                'view_name': 'marketplace-public-plan-detail',
            },
        }

    provider_name = serializers.ReadOnlyField(source='offering.customer.name')
    provider_uuid = serializers.ReadOnlyField(source='offering.customer.uuid')
    category_title = serializers.ReadOnlyField(source='offering.category.title')
    category_icon = ProtectedImageField(source='offering.category.icon', read_only=True)
    category_uuid = serializers.ReadOnlyField(source='offering.category.uuid')
    offering_thumbnail = ProtectedFileField(source='offering.thumbnail', read_only=True)
    offering_image = ProtectedFileField(source='offering.image', read_only=True)

    def validate_offering(self, offering):
        if not offering.state == models.Offering.States.ACTIVE:
            raise rf_exceptions.ValidationError(_('Offering is not available.'))
        return offering

    def validate(self, attrs):
        offering = attrs.get('offering')
        plan = attrs.get('plan')

        if not offering:
            if not self.instance:
                raise rf_exceptions.ValidationError(
                    {'offering': _('This field is required.')}
                )
            offering = self.instance.offering

        if plan:
            if plan.offering != offering:
                raise rf_exceptions.ValidationError(
                    {'plan': _('This plan is not available for selected offering.')}
                )

            validate_plan(plan)

        if offering.options:
            validate_options(
                offering.options.get('options', {}), attrs.get('attributes')
            )

        limits = attrs.get('limits')
        if limits:
            utils.validate_limits(limits, offering)
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
            'resource_uuid',
            'resource_type',
            'resource_name',
            'cost',
            'state',
            'output',
            'marketplace_resource_uuid',
            'error_message',
            'error_traceback',
            'accepting_terms_of_service',
            'callback_url',
        )

        read_only_fields = (
            'cost',
            'state',
            'error_message',
            'error_traceback',
            'output',
        )
        protected_fields = ('offering', 'plan', 'callback_url')

    marketplace_resource_uuid = serializers.ReadOnlyField(source='resource.uuid')
    resource_name = serializers.ReadOnlyField(source='resource.name')
    resource_uuid = serializers.ReadOnlyField(source='resource.backend_uuid')
    resource_type = serializers.ReadOnlyField(source='resource.backend_type')
    state = serializers.ReadOnlyField(source='get_state_display')
    limits = serializers.DictField(child=serializers.IntegerField(), required=False)
    accepting_terms_of_service = serializers.BooleanField(
        required=False, write_only=True
    )

    def get_fields(self):
        fields = super(NestedOrderItemSerializer, self).get_fields()
        method = self.context['view'].request.method

        user = self.context['view'].request.user
        # conceal detailed error message from non-system users
        if not user.is_staff and not user.is_support and 'error_traceback' in fields:
            del fields['error_traceback']

        if method == 'GET' and 'attributes' in fields:
            fields['attributes'] = serializers.ReadOnlyField(source='safe_attributes')
        return fields


class OrderItemDetailsSerializer(NestedOrderItemSerializer):
    class Meta(NestedOrderItemSerializer.Meta):
        fields = NestedOrderItemSerializer.Meta.fields + (
            'order_uuid',
            'order_approved_at',
            'order_approved_by',
            'created_by_full_name',
            'created_by_civil_number',
            'customer_name',
            'customer_uuid',
            'project_name',
            'project_uuid',
            'project_description',
            'old_plan_name',
            'new_plan_name',
            'old_plan_uuid',
            'new_plan_uuid',
            'old_cost_estimate',
            'new_cost_estimate',
            'can_terminate',
            'fixed_price',
            'activation_price',
            'reviewed_by',
            'reviewed_at',
        )

    order_uuid = serializers.ReadOnlyField(source='order.uuid')
    order_approved_at = serializers.ReadOnlyField(source='order.approved_at')
    order_approved_by = serializers.ReadOnlyField(source='order.approved_by.full_name')

    reviewed_by = serializers.ReadOnlyField(source='reviewed_by.username')

    created_by_full_name = serializers.ReadOnlyField(
        source='order.created_by.full_name'
    )
    created_by_civil_number = serializers.ReadOnlyField(
        source='order.created_by.civil_number'
    )

    customer_name = serializers.SerializerMethodField()
    customer_uuid = serializers.SerializerMethodField()

    project_name = serializers.SerializerMethodField()
    project_uuid = serializers.SerializerMethodField()
    project_description = serializers.SerializerMethodField()

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

        if order_item.state not in (
            models.OrderItem.States.PENDING,
            models.OrderItem.States.EXECUTING,
        ):
            return False

        return True

    @lru_cache(maxsize=1)
    def _get_project(self, order_item: models.OrderItem):
        return order_item.order.project

    def get_customer_uuid(self, order_item: models.OrderItem):
        project = self._get_project(order_item)
        return project.customer.uuid

    def get_customer_name(self, order_item: models.OrderItem):
        project = self._get_project(order_item)
        return project.customer.name

    def get_project_uuid(self, order_item: models.OrderItem):
        project = self._get_project(order_item)
        return project.uuid

    def get_project_name(self, order_item: models.OrderItem):
        project = self._get_project(order_item)
        return project.name

    def get_project_description(self, order_item: models.OrderItem):
        project = self._get_project(order_item)
        return project.description


class OrderItemSetStateErredSerializer(
    serializers.ModelSerializer, core_serializers.AugmentedSerializerMixin
):
    class Meta:
        model = models.OrderItem
        fields = ('error_message', 'error_traceback')
        protected_fields = ('error_message', 'error_traceback')


class CartItemSerializer(BaseRequestSerializer):
    limits = serializers.DictField(child=serializers.IntegerField(), required=False)
    estimate = serializers.ReadOnlyField(source='cost')
    project = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name='project-detail',
        queryset=structure_models.Project.available_objects.all(),
    )
    project_uuid = serializers.ReadOnlyField(source='project.uuid')
    project_name = serializers.ReadOnlyField(source='project.name')

    class Meta(BaseRequestSerializer.Meta):
        model = models.CartItem
        fields = BaseRequestSerializer.Meta.fields + (
            'estimate',
            'project',
            'project_name',
            'project_uuid',
            'fixed_price',
            'activation_price',
        )
        protected_fields = BaseRequestSerializer.Meta.protected_fields + ('project',)

    def get_fields(self):
        fields = super(CartItemSerializer, self).get_fields()
        if 'project' in fields:
            fields['project'].queryset = filter_queryset_for_user(
                fields['project'].queryset, self.context['request'].user
            )
        return fields

    def quotas_validate(self, item, project):
        try:
            with transaction.atomic():
                processor_class = manager.get_processor(
                    item.offering.type, 'create_resource_processor'
                )
                order_params = dict(
                    project=project, created_by=self.context['request'].user
                )
                order = models.Order(**order_params)
                item_params = get_item_params(item)
                order_item = models.OrderItem(order=order, **item_params)

                if issubclass(processor_class, CreateResourceProcessor):
                    processor = processor_class(order_item)
                    post_data = processor.get_post_data()
                    serializer_class = processor.get_serializer_class()
                    if serializer_class:
                        serializer = serializer_class(
                            data=post_data, context=self.context
                        )
                        serializer.is_valid(raise_exception=True)
                        serializer.save()
                        raise exceptions.TransactionRollback()
        except exceptions.TransactionRollback:
            pass

    @transaction.atomic
    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        item = super(CartItemSerializer, self).create(validated_data)
        item.init_cost()
        item.save(update_fields=['cost'])
        self.quotas_validate(item, validated_data['project'])
        return item

    @transaction.atomic
    def update(self, instance, validated_data):
        instance = super(CartItemSerializer, self).update(instance, validated_data)
        instance.init_cost()
        instance.save(update_fields=['cost'])
        return instance


class CartSubmitSerializer(serializers.Serializer):
    project = serializers.HyperlinkedRelatedField(
        queryset=structure_models.Project.available_objects.all(),
        view_name='project-detail',
        lookup_field='uuid',
        required=True,
    )

    def get_fields(self):
        fields = super(CartSubmitSerializer, self).get_fields()
        project_field = fields['project']
        project_field.queryset = filter_queryset_for_user(
            project_field.queryset, self.context['request'].user
        )
        return fields

    @transaction.atomic()
    def create(self, validated_data):
        user = self.context['request'].user
        project = validated_data['project']

        items = models.CartItem.objects.filter(user=user, project=project)
        if items.count() == 0:
            raise serializers.ValidationError(_('Shopping cart is empty'))

        order = create_order(project, user, items, self.context['request'])
        items.delete()
        return order


def get_item_params(item):
    return dict(
        offering=item.offering,
        attributes=item.attributes,
        resource=getattr(item, 'resource', None),  # cart item does not have resource
        plan=item.plan,
        old_plan=getattr(item, 'old_plan', None),  # cart item does not have old plan
        limits=item.limits,
        type=item.type,
    )


def create_order(project, user, items, request):
    for item in items:
        if (
            item.type
            in (models.OrderItem.Types.UPDATE, models.OrderItem.Types.TERMINATE)
            and item.resource
        ):
            utils.check_pending_order_item_exists(item.resource)

    order_params = dict(project=project, created_by=user)
    order = models.Order.objects.create(**order_params)

    for item in items:
        try:
            params = get_item_params(item)
            order_item = order.add_item(**params)
        except ValidationError as e:
            raise rf_exceptions.ValidationError(e)
        utils.validate_order_item(order_item, request)

    order.init_total_cost()
    order.save()

    if check_availability_of_auto_approving(items, user, project):
        tasks.approve_order(order, user)
    else:
        transaction.on_commit(lambda: tasks.notify_order_approvers.delay(order.uuid))

    return order


class OrderSerializer(
    structure_serializers.PermissionFieldFilteringMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):

    state = serializers.ReadOnlyField(source='get_state_display')
    items = NestedOrderItemSerializer(many=True)
    customer_uuid = serializers.ReadOnlyField(source='project.customer.uuid')
    project_name = serializers.ReadOnlyField(source='project.name')
    project_description = serializers.ReadOnlyField(source='project.description')
    customer_name = serializers.ReadOnlyField(source='project.customer.name')

    class Meta:
        model = models.Order
        fields = (
            'url',
            'uuid',
            'created',
            'created_by',
            'created_by_username',
            'created_by_full_name',
            'approved_by',
            'approved_at',
            'approved_by_username',
            'approved_by_full_name',
            'project',
            'project_uuid',
            'project_name',
            'project_description',
            'customer_name',
            'customer_uuid',
            'state',
            'items',
            'total_cost',
            'file',
            'type',
            'error_message',
        )
        read_only_fields = (
            'created_by',
            'approved_by',
            'approved_at',
            'state',
            'total_cost',
        )
        protected_fields = ('project', 'items')
        related_paths = {
            'created_by': ('username', 'full_name'),
            'approved_by': ('username', 'full_name'),
            'project': ('uuid',),
        }
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'created_by': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
            'approved_by': {'lookup_field': 'uuid', 'view_name': 'user-detail'},
            'project': {'lookup_field': 'uuid', 'view_name': 'project-detail'},
        }

    file = serializers.SerializerMethodField()

    def get_file(self, obj):
        return reverse(
            'marketplace-order-pdf',
            kwargs={'uuid': obj.uuid.hex},
            request=self.context['request'],
        )

    error_message = serializers.SerializerMethodField()

    def get_error_message(self, obj: models.Order):
        return '\n'.join(
            [f'{item.uuid}: {item.error_message}' for item in obj.items.all()]
        )

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
        return ('project',)

    def validate_items(self, items):
        for item in items:
            offering = item['offering']

            if (
                offering.shared
                and offering.terms_of_service
                and not item.get('accepting_terms_of_service')
            ):
                raise ValidationError(
                    _('Terms of service for offering \'%s\' have not been accepted.')
                    % offering
                )
        return items

    def validate(self, attrs):
        project = attrs['project']

        for item in attrs['items']:
            offering = item['offering']

            if offering.shared and offering.divisions.count():
                if (
                    not project.customer.division_id
                    or not offering.divisions.filter(
                        id=project.customer.division_id
                    ).exists()
                ):
                    raise ValidationError(
                        _('This offering is not available for ordering.')
                    )

        return attrs


class ResourceSerializer(BaseItemSerializer):
    class Meta(BaseItemSerializer.Meta):
        model = models.Resource
        fields = BaseItemSerializer.Meta.fields + (
            'url',
            'scope',
            'description',
            'state',
            'resource_uuid',
            'backend_id',
            'effective_id',
            'access_url',
            'resource_type',
            'project',
            'project_uuid',
            'project_name',
            'project_description',
            'customer_uuid',
            'customer_name',
            'offering_uuid',
            'offering_name',
            'parent_uuid',
            'parent_name',
            'backend_metadata',
            'is_usage_based',
            'is_limit_based',
            'name',
            'current_usages',
            'can_terminate',
            'report',
            'end_date',
            'username',
            'limit_usage',
        )
        read_only_fields = (
            'backend_metadata',
            'scope',
            'current_usages',
            'backend_id',
            'effective_id',
            'access_url',
            'report',
            'description',
            'limit_usage',
        )
        view_name = 'marketplace-resource-detail'
        extra_kwargs = dict(
            **BaseItemSerializer.Meta.extra_kwargs, url={'lookup_field': 'uuid'}
        )

    state = serializers.ReadOnlyField(source='get_state_display')
    scope = core_serializers.GenericRelatedField()
    resource_uuid = serializers.ReadOnlyField(source='backend_uuid')
    resource_type = serializers.ReadOnlyField(source='backend_type')
    access_url = serializers.ReadOnlyField(source='offering.access_url')
    project = serializers.HyperlinkedRelatedField(
        lookup_field='uuid',
        view_name='project-detail',
        read_only=True,
    )
    project_uuid = serializers.ReadOnlyField(source='project.uuid')
    project_name = serializers.ReadOnlyField(source='project.name')
    project_description = serializers.ReadOnlyField(source='project.description')
    customer_name = serializers.ReadOnlyField(source='project.customer.name')
    customer_uuid = serializers.ReadOnlyField(source='project.customer.uuid')
    offering_uuid = serializers.ReadOnlyField(source='offering.uuid')
    offering_name = serializers.ReadOnlyField(source='offering.name')
    parent_uuid = serializers.ReadOnlyField(source='parent.uuid')
    parent_name = serializers.ReadOnlyField(source='parent.name')
    # If resource is usage-based, frontend would render button to show and report usage
    is_usage_based = serializers.ReadOnlyField(source='offering.is_usage_based')
    is_limit_based = serializers.ReadOnlyField(source='offering.is_limit_based')
    can_terminate = serializers.SerializerMethodField()
    report = serializers.JSONField(read_only=True)
    username = serializers.SerializerMethodField()
    limit_usage = serializers.SerializerMethodField()

    def get_can_terminate(self, resource):
        view = self.context['view']
        try:
            permissions.user_can_terminate_resource(view.request, view, resource)
        except APIException:
            return False
        except ObjectDoesNotExist:
            return False
        validator = core_validators.StateValidator(
            models.Resource.States.OK, models.Resource.States.ERRED
        )
        try:
            validator(resource)
        except APIException:
            return False

        try:
            structure_utils.check_customer_blocked_or_archived(resource.project)
        except ValidationError:
            return False

        if models.OrderItem.objects.filter(
            resource=resource,
            state__in=(
                models.OrderItem.States.PENDING,
                models.OrderItem.States.EXECUTING,
            ),
        ).exists():
            return False
        return True

    def get_username(self, resource):
        user = self.context['request'].user
        offering_user = models.OfferingUser.objects.filter(
            offering=resource.offering, user=user
        ).first()
        if offering_user:
            return offering_user.username

    def get_limit_usage(self, resource):
        if not resource.offering.is_limit_based or not resource.plan:
            return

        limit_usage = {}

        for plan_component in resource.plan.components.all():
            if (
                plan_component.component.billing_type
                == models.OfferingComponent.BillingTypes.LIMIT
            ):
                if (
                    plan_component.component.limit_period
                    == models.OfferingComponent.LimitPeriods.TOTAL
                ):
                    limit_usage[
                        plan_component.component.type
                    ] = models.ComponentUsage.objects.filter(
                        resource=resource,
                        component=plan_component.component,
                    ).aggregate(
                        total=Sum('usage')
                    )[
                        'total'
                    ]

                if (
                    plan_component.component.limit_period
                    == models.OfferingComponent.LimitPeriods.ANNUAL
                ):
                    year_start = datetime.date(
                        year=datetime.date.today().year, month=1, day=1
                    )
                    limit_usage[
                        plan_component.component.type
                    ] = models.ComponentUsage.objects.filter(
                        resource=resource,
                        component=plan_component.component,
                        date__gte=year_start,
                    ).aggregate(
                        total=Sum('usage')
                    )[
                        'total'
                    ]

        return limit_usage


class ResourceSwitchPlanSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Resource
        fields = ('plan',)

    plan = serializers.HyperlinkedRelatedField(
        view_name='marketplace-public-plan-detail',
        lookup_field='uuid',
        queryset=models.Plan.objects.all(),
        required=True,
    )

    def validate(self, attrs):
        plan = attrs['plan']
        resource = self.context['view'].get_object()

        if plan.offering != resource.offering:
            raise rf_exceptions.ValidationError(
                {'plan': _('Plan is not available for this offering.')}
            )

        validate_plan(plan)
        return attrs


class ResourceUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ('name', 'description', 'end_date')

    def validate_end_date(self, end_date):
        if not end_date:
            return
        if not settings.WALDUR_MARKETPLACE['ENABLE_RESOURCE_END_DATE']:
            raise serializers.ValidationError(
                {'end_date': _('Update of this field is not allowed.')}
            )
        if end_date < timezone.datetime.today().date():
            raise serializers.ValidationError(
                {'end_date': _('Cannot be earlier than the current date.')}
            )
        return end_date

    def save(self, **kwargs):
        resource = super(ResourceUpdateSerializer, self).save(**kwargs)
        user = self.context['request'].user

        if 'end_date' in self.validated_data:
            log.log_marketplace_resource_end_date_has_been_updated(resource, user)


class ResourceEndDateByProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ('end_date',)

    def validate_end_date(self, end_date):
        if not end_date:
            return
        if not settings.WALDUR_MARKETPLACE['ENABLE_RESOURCE_END_DATE']:
            raise serializers.ValidationError(
                {'end_date': _('Update of this field is not allowed.')}
            )
        invoice_threshold = timezone.datetime.today() - datetime.timedelta(days=90)
        if InvoiceItem.objects.filter(
            invoice__created__gt=invoice_threshold, resource=self.instance
        ).exists():
            raise serializers.ValidationError(
                _(
                    'Service provider can not set end date of the resource which has been used for the last 90 days.'
                )
            )

        min_end_date = timezone.datetime.today() + datetime.timedelta(days=7)
        if end_date < min_end_date.date():
            raise serializers.ValidationError(
                _('Please set at least 7 days in advance.')
            )
        return end_date


class ResourceUpdateLimitsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ('limits',)

    limits = serializers.DictField(
        child=serializers.IntegerField(min_value=0), required=True
    )


class ResourceBackendIDSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ('backend_id',)


class ResourceStateSerializer(serializers.Serializer):
    state = serializers.ChoiceField(['ok', 'erred', 'terminated'])


class ReportSectionSerializer(serializers.Serializer):
    header = serializers.CharField()
    body = serializers.CharField()


class ResourceReportSerializer(serializers.Serializer):
    report = ReportSectionSerializer(many=True)

    def validate_report(self, report):
        if len(report) == 0:
            raise serializers.ValidationError(
                'Report object should contain at least one section.'
            )

        return report


class ResourceOfferingSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Offering
        fields = ('name', 'uuid')


class BaseComponentSerializer(serializers.Serializer):
    type = serializers.ReadOnlyField(source='component.type')
    name = serializers.ReadOnlyField(source='component.name')
    measured_unit = serializers.ReadOnlyField(source='component.measured_unit')


class CategoryComponentUsageSerializer(
    core_serializers.RestrictedSerializerMixin,
    BaseComponentSerializer,
    serializers.ModelSerializer,
):
    category_title = serializers.ReadOnlyField(source='component.category.title')
    category_uuid = serializers.ReadOnlyField(source='component.category.uuid')
    scope = GenericRelatedField(
        related_models=(structure_models.Project, structure_models.Customer)
    )

    class Meta:
        model = models.CategoryComponentUsage
        fields = (
            'name',
            'type',
            'measured_unit',
            'category_title',
            'category_uuid',
            'date',
            'reported_usage',
            'fixed_usage',
            'scope',
        )


class BaseComponentUsageSerializer(
    BaseComponentSerializer, serializers.ModelSerializer
):
    class Meta:
        model = models.ComponentUsage
        fields = (
            'uuid',
            'created',
            'description',
            'type',
            'name',
            'measured_unit',
            'usage',
            'date',
            'recurring',
        )


class ComponentUsageSerializer(BaseComponentUsageSerializer):
    resource_name = serializers.ReadOnlyField(source='resource.name')
    resource_uuid = serializers.ReadOnlyField(source='resource.uuid')

    offering_name = serializers.ReadOnlyField(source='resource.offering.name')
    offering_uuid = serializers.ReadOnlyField(source='resource.offering.uuid')

    project_uuid = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()

    customer_name = serializers.SerializerMethodField()
    customer_uuid = serializers.SerializerMethodField()

    class Meta(BaseComponentUsageSerializer.Meta):
        fields = BaseComponentUsageSerializer.Meta.fields + (
            'resource_name',
            'resource_uuid',
            'offering_name',
            'offering_uuid',
            'project_name',
            'project_uuid',
            'customer_name',
            'customer_uuid',
            'recurring',
            'billing_period',
        )

    def get_project_uuid(self, instance):
        return instance.resource.project.uuid

    def get_project_name(self, instance):
        return instance.resource.project.name

    def get_customer_uuid(self, instance):
        return instance.resource.project.customer.uuid

    def get_customer_name(self, instance):
        return instance.resource.project.customer.name


class ResourcePlanPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ResourcePlanPeriod
        fields = ('uuid', 'plan_name', 'plan_uuid', 'start', 'end', 'components')

    plan_name = serializers.ReadOnlyField(source='plan.name')
    plan_uuid = serializers.ReadOnlyField(source='plan.uuid')
    components = BaseComponentUsageSerializer(source='current_components', many=True)


class ImportResourceSerializer(serializers.Serializer):
    backend_id = serializers.CharField()
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.available_objects.all(), slug_field='uuid'
    )
    plan = serializers.SlugRelatedField(
        queryset=models.Plan.objects.all(), slug_field='uuid', required=False
    )

    def get_fields(self):
        fields = super(ImportResourceSerializer, self).get_fields()

        request = self.context['request']
        user = request.user
        fields['project'].queryset = filter_queryset_for_user(
            fields['project'].queryset, user
        )
        return fields


class ServiceProviderSignatureSerializer(serializers.Serializer):
    customer = serializers.SlugRelatedField(
        queryset=structure_models.Customer.objects.all(), slug_field='uuid'
    )
    data = serializers.CharField()
    dry_run = serializers.BooleanField(default=False, required=False)

    def validate(self, attrs):
        customer = attrs['customer']
        service_provider = getattr(customer, 'serviceprovider', None)
        api_secret_code = service_provider and service_provider.api_secret_code

        if not api_secret_code:
            raise rf_exceptions.ValidationError(_('API secret code is not set.'))

        try:
            data = core_utils.decode_jwt_token(attrs['data'], api_secret_code)
            attrs['data'] = data
            return attrs
        except jwt.exceptions.DecodeError:
            raise rf_exceptions.ValidationError(_('Signature verification failed.'))


class ComponentUsageItemSerializer(serializers.Serializer):
    type = serializers.CharField()
    amount = serializers.IntegerField()
    description = serializers.CharField(required=False, allow_blank=True)
    recurring = serializers.BooleanField(default=False)


class ComponentUsageCreateSerializer(serializers.Serializer):
    usages = ComponentUsageItemSerializer(many=True)
    plan_period = serializers.SlugRelatedField(
        queryset=models.ResourcePlanPeriod.objects.all(), slug_field='uuid'
    )

    def validate_plan_period(self, plan_period):
        date = datetime.date.today()
        if plan_period.end and plan_period.end < core_utils.month_start(date):
            raise serializers.ValidationError(_('Billing period is closed.'))
        return plan_period

    @classmethod
    def get_components_map(cls, offering) -> Dict[str, models.OfferingComponent]:
        # Allow to report usage for limit-based components
        components = offering.components.filter(
            billing_type__in=[BillingTypes.USAGE, BillingTypes.LIMIT]
        )
        return {component.type: component for component in components}

    def validate(self, attrs):
        attrs = super(ComponentUsageCreateSerializer, self).validate(attrs)
        plan_period = attrs['plan_period']
        resource = plan_period.resource
        offering = resource.plan.offering

        States = models.Resource.States
        if resource.state not in (States.OK, States.UPDATING, States.TERMINATING):
            raise rf_exceptions.ValidationError(
                {'resource': _('Resource is not in valid state.')}
            )

        valid_components = set(self.get_components_map(offering))
        actual_components = {usage['type'] for usage in attrs['usages']}

        invalid_components = ', '.join(sorted(actual_components - valid_components))

        if invalid_components:
            raise rf_exceptions.ValidationError(
                _('These components are invalid: %s.') % invalid_components
            )

        return attrs

    def save(self):
        plan_period = self.validated_data['plan_period']
        resource = plan_period.resource
        components_map = self.get_components_map(resource.plan.offering)
        now = timezone.now()
        billing_period = core_utils.month_start(now)

        for usage in self.validated_data['usages']:
            amount = usage['amount']
            description = usage.get('description', '')
            component = components_map[usage['type']]
            recurring = usage['recurring']
            if component.billing_type == models.OfferingComponent.BillingTypes.USAGE:
                component.validate_amount(resource, amount, now)

            models.ComponentUsage.objects.filter(
                resource=resource,
                component=component,
                billing_period=billing_period,
            ).update(recurring=False)

            usage, created = models.ComponentUsage.objects.update_or_create(
                resource=resource,
                component=component,
                plan_period=plan_period,
                billing_period=billing_period,
                defaults={
                    'usage': amount,
                    'date': now,
                    'description': description,
                    'recurring': recurring,
                },
            )
            if created:
                message = 'Usage has been created for %s, component: %s, value: %s' % (
                    resource,
                    component.type,
                    amount,
                )
                logger.info(message)
                log.log_component_usage_creation_succeeded(usage)
            else:
                message = 'Usage has been updated for %s, component: %s, value: %s' % (
                    resource,
                    component.type,
                    amount,
                )
                logger.info(message)
                log.log_component_usage_update_succeeded(usage)
        resource.current_usages = {
            usage['type']: usage['amount'] for usage in self.validated_data['usages']
        }
        resource.save(update_fields=['current_usages'])


class OfferingFileSerializer(
    MarketplaceProtectedMediaSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.OfferingFile
        fields = (
            'url',
            'uuid',
            'name',
            'offering',
            'created',
            'file',
        )
        extra_kwargs = dict(
            url={'lookup_field': 'uuid'},
            offering={
                'lookup_field': 'uuid',
                'view_name': 'marketplace-provider-offering-detail',
            },
        )


class OfferingReferralSerializer(
    serializers.HyperlinkedModelSerializer,
    core_serializers.AugmentedSerializerMixin,
):
    scope = GenericRelatedField(read_only=True)
    scope_uuid = serializers.ReadOnlyField(source='scope.uuid')

    class Meta:
        model = pid_models.DataciteReferral
        fields = (
            'url',
            'uuid',
            'scope',
            'scope_uuid',
            'pid',
            'relation_type',
            'resource_type',
            'creator',
            'publisher',
            'published',
            'title',
            'referral_url',
        )
        extra_kwargs = dict(
            url={
                'lookup_field': 'uuid',
                'view_name': 'marketplace-offering-referral-detail',
            },
            offering={
                'lookup_field': 'uuid',
                'view_name': 'marketplace-provider-offering-detail',
            },
        )


class OfferingUserSerializer(serializers.HyperlinkedModelSerializer):
    offering_uuid = serializers.ReadOnlyField(source='offering.uuid')
    offering_name = serializers.ReadOnlyField(source='offering.name')
    user_uuid = serializers.ReadOnlyField(source='user.uuid')
    user_username = serializers.ReadOnlyField(source='user.username')

    class Meta:
        model = models.OfferingUser
        fields = (
            'user',
            'offering',
            'username',
            'offering_uuid',
            'offering_name',
            'user_uuid',
            'user_username',
            'created',
            'modified',
        )
        extra_kwargs = dict(
            offering={
                'lookup_field': 'uuid',
                'view_name': 'marketplace-provider-offering-detail',
            },
            user={'lookup_field': 'uuid', 'view_name': 'user-detail'},
        )

    def create(self, validated_data):
        user = self.context['request'].user
        offering = validated_data['offering']

        if not user.is_staff and not offering.customer.has_user(
            user, structure_models.CustomerRole.OWNER
        ):
            raise rf_exceptions.ValidationError(
                _('You do not have permission to create offering user.')
            )

        if not offering.secret_options.get('service_provider_can_create_offering_user'):
            raise rf_exceptions.ValidationError(
                _('It is not allowed to create users for current offering.')
            )

        return super(OfferingUserSerializer, self).create(validated_data)


def validate_plan(plan):
    """ "
    Ensure that maximum amount of resources with current plan is not reached yet.
    """
    if not plan.is_active:
        raise rf_exceptions.ValidationError(
            {'plan': _('Plan is not available because limit has been reached.')}
        )


def get_is_service_provider(serializer, scope):
    customer = structure_permissions._get_customer(scope)
    return models.ServiceProvider.objects.filter(customer=customer).exists()


def add_service_provider(sender, fields, **kwargs):
    fields['is_service_provider'] = serializers.SerializerMethodField()
    setattr(sender, 'get_is_service_provider', get_is_service_provider)


class ResourceTerminateSerializer(serializers.Serializer):
    attributes = serializers.JSONField(
        label=_('Termination attributes'), required=False
    )


class MoveResourceSerializer(serializers.Serializer):
    project = structure_serializers.NestedProjectSerializer(
        queryset=structure_models.Project.available_objects.all(),
        required=True,
        many=False,
    )


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_service_provider,
)


def get_marketplace_resource_count(serializer, project):
    counts = (
        models.Resource.objects.order_by()
        .filter(
            state__in=(models.Resource.States.OK, models.Resource.States.UPDATING),
            project=project,
        )
        .values('offering__category__uuid')
        .annotate(count=Count('*'))
    )
    return {str(c['offering__category__uuid']): c['count'] for c in list(counts)}


def add_marketplace_resource_count(sender, fields, **kwargs):
    fields['marketplace_resource_count'] = serializers.SerializerMethodField()
    setattr(sender, 'get_marketplace_resource_count', get_marketplace_resource_count)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.ProjectSerializer,
    receiver=add_marketplace_resource_count,
)


class OfferingThumbnailSerializer(
    MarketplaceProtectedMediaSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    thumbnail = serializers.ImageField(required=True)

    class Meta:
        model = models.Offering
        fields = ('thumbnail',)


class DivisionsSerializer(serializers.Serializer):
    divisions = serializers.HyperlinkedRelatedField(
        queryset=structure_models.Division.objects.all(),
        view_name='division-detail',
        lookup_field='uuid',
        required=False,
        many=True,
    )

    def save(self, **kwargs):
        if isinstance(self.instance, models.Offering):
            offering = self.instance
            divisions = self.validated_data['divisions']
            offering.divisions.clear()

            if divisions:
                offering.divisions.add(*divisions)
        elif isinstance(self.instance, models.Plan):
            plan = self.instance
            divisions = self.validated_data['divisions']
            plan.divisions.clear()

            if divisions:
                plan.divisions.add(*divisions)


class CostsSerializer(serializers.Serializer):
    period = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    tax = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    def get_period(self, record):
        return '%s-%02d' % (record['invoice__year'], record['invoice__month'])

    def get_total(self, record):
        return round(record['computed_tax'] + record['computed_price'], 2)

    def get_price(self, record):
        return round(record['computed_price'], 2)

    def get_tax(self, record):
        return round(record['computed_tax'], 2)


class OfferingCostSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(source='resource__offering__uuid')
    cost = serializers.FloatField()


class OfferingComponentStatSerializer(serializers.Serializer):
    period = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    measured_unit = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()

    def get_date(self, record):
        date = parse_datetime(self.get_period(record))
        # for consistency with usage resource usage reporting, assume values at the beginning of the last day
        return (
            core_utils.month_end(date)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )

    def get_usage(self, record):
        return record['total_quantity']

    def get_period(self, record):
        return '%s-%02d' % (record['invoice__year'], record['invoice__month'])

    def get_component_attr(self, record, attrname):
        component = self.context['offering_components_map'].get(
            record['details__offering_component_type']
        )
        return component and getattr(component, attrname)

    def get_description(self, record):
        return self.get_component_attr(record, 'description')

    def get_measured_unit(self, record):
        return self.get_component_attr(record, 'measured_unit')

    def get_type(self, record):
        return self.get_component_attr(record, 'type')

    def get_name(self, record):
        return self.get_component_attr(record, 'name')


class CountStatsSerializer(serializers.Serializer):
    name = serializers.SerializerMethodField()
    uuid = serializers.SerializerMethodField()
    count = serializers.SerializerMethodField()

    def _get_value(self, record, name):
        for k in record.keys():
            if name in k:
                return record[k]

    def get_name(self, record):
        return self._get_value(record, 'name')

    def get_uuid(self, record):
        return self._get_value(record, 'uuid')

    def get_count(self, record):
        return self._get_value(record, 'count')


class CustomerStatsSerializer(CountStatsSerializer):
    abbreviation = serializers.SerializerMethodField()

    def get_abbreviation(self, record):
        return self._get_value(record, 'abbreviation')


class CustomerOecdCodeStatsSerializer(CustomerStatsSerializer):
    oecd = serializers.CharField(source='oecd_fos_2007_name')


class CustomerIndustryFlagStatsSerializer(CustomerStatsSerializer):
    is_industry = serializers.CharField()


class OfferingCountryStatsSerializer(serializers.Serializer):
    country = serializers.CharField(source='offering__country')
    count = serializers.IntegerField()


class ComponentUsagesStatsSerializer(serializers.Serializer):
    usage = serializers.IntegerField()
    offering_uuid = serializers.CharField(source='resource__offering__uuid')
    component_type = serializers.CharField(source='component__type')


class ComponentUsagesPerMonthStatsSerializer(ComponentUsagesStatsSerializer):
    month = serializers.IntegerField(source='billing_period__month')
    year = serializers.IntegerField(source='billing_period__year')


class OfferingStatsSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    name = serializers.CharField(source='offering__name')
    uuid = serializers.CharField(source='offering__uuid')
    country = serializers.CharField(source='offering__country')
