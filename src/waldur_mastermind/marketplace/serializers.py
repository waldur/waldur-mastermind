import datetime
import logging
from decimal import Decimal
from typing import Literal

import jwt
from dateutil.parser import parse as parse_datetime
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Count, QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.drainage import set_override
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers
from rest_framework.exceptions import APIException, PermissionDenied

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core.clean_html import clean_html
from waldur_core.core.enums import CoreStates, CoreStateType
from waldur_core.core.fields import NaturalChoiceField
from waldur_core.core.mixins import GetValueMixin
from waldur_core.core.models import User, get_ssh_key_fingerprints
from waldur_core.core.validators import BackendURLValidator, validate_ssh_public_key
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import (
    count_users,
    get_permissions,
    has_permission,
)
from waldur_core.quotas.serializers import QuotaSerializer
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure import utils as structure_utils
from waldur_core.structure.executors import ServiceSettingsCreateExecutor
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.serializers import get_options_serializer_class
from waldur_mastermind.billing.serializers import (
    NestedPriceEstimateSerializer,
    get_payment_profiles,
)
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.exceptions import TransactionRollback
from waldur_mastermind.common.serializers import validate_options
from waldur_mastermind.common.utils import prices_are_equal
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.invoices.serializers import PaymentProfileSerializer
from waldur_mastermind.invoices.utils import get_billing_price_estimate_for_resources
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    OrderStatesType,
    ResourceStates,
    ResourceStatesType,
)
from waldur_mastermind.marketplace.fields import PublicPlanField
from waldur_mastermind.marketplace.managers import ResourceQuerySet
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.processors import CreateResourceProcessor
from waldur_mastermind.marketplace.utils import (
    UsernameGenerationPolicy,
    get_service_provider_resources,
    get_service_provider_user_ids,
    validate_attributes,
    validate_end_date,
)
from waldur_mastermind.marketplace_openstack import TENANT_TYPE
from waldur_mastermind.proposal import models as proposal_models
from waldur_pid import models as pid_models

from . import log, models, permissions, plugins, utils

logger = logging.getLogger(__name__)
BillingTypes = models.OfferingComponent.BillingTypes


class LifecyclePluginOptionsSerializer(serializers.Serializer):
    auto_approve_remote_orders = serializers.BooleanField(
        required=False,
        help_text="If set to True, an order can be processed without approval",
    )

    service_provider_can_create_offering_user = serializers.BooleanField(
        required=False, help_text="Service provider can create offering user"
    )

    max_resource_termination_offset_in_days = serializers.IntegerField(
        required=False,
        min_value=0,
        help_text="Maximum resource termination offset in days",
    )
    default_resource_termination_offset_in_days = serializers.IntegerField(
        required=False,
        min_value=0,
        help_text="If set, it will be used as a default resource termination offset in days",
    )
    is_resource_termination_date_required = serializers.BooleanField(
        required=False,
        help_text="If set to True, resource termination date is required",
    )
    latest_date_for_resource_termination = serializers.DateField(
        required=False,
        help_text="If set, it will be used as a latest date for resource termination",
    )
    auto_approve_in_service_provider_projects = serializers.BooleanField(
        required=False,
        help_text="Skip approval of public offering belonging to the same organization under which the request is done",
    )
    supports_downscaling = serializers.BooleanField(
        required=False,
        help_text="If set to True, it will be possible to downscale resources",
    )
    supports_pausing = serializers.BooleanField(
        required=False,
        help_text="If set to True, it will be possible to pause resources",
    )


class SupportPluginOptionsSerializer(serializers.Serializer):
    enable_issues_for_membership_changes = serializers.BooleanField(
        required=False,
        help_text="Enable issues for membership changes",
    )


class OpenStackPluginOptionsSerializer(serializers.Serializer):
    default_internal_network_mtu = serializers.IntegerField(
        required=False,
        min_value=68,
        max_value=9000,
        help_text="If set, it will be used as a default MTU for the first network in a tenant",
    )
    max_instances = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Default limit for number of instances in OpenStack tenant",
    )
    max_volumes = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Default limit for number of volumes in OpenStack tenant",
    )
    storage_mode = serializers.ChoiceField(
        required=False,
        choices=["fixed", "dynamic"],
        help_text="Storage mode for OpenStack offering",
    )
    snapshot_size_limit_gb = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Default limit for snapshot size in GB",
    )


class HeappePluginOptionsSerializer(serializers.Serializer):
    heappe_cluster_id = serializers.CharField(
        required=False, help_text="HEAppE cluster id"
    )
    heappe_local_base_path = serializers.CharField(
        required=False, help_text="HEAppE local base path"
    )
    heappe_url = serializers.CharField(required=False, help_text="HEAppE url")
    heappe_username = serializers.CharField(required=False, help_text="HEAppE username")
    homedir_prefix = serializers.CharField(
        required=False, help_text="GLAuth homedir prefix", default="/home/"
    )


class GLAuthPluginOptionsSerializer(serializers.Serializer):
    initial_primarygroup_number = serializers.IntegerField(
        required=False,
        default=5000,
        help_text="GLAuth initial primary group number",
        min_value=0,
    )

    initial_uidnumber = serializers.IntegerField(
        required=False, default=5000, help_text="GLAuth initial uidnumber", min_value=0
    )
    initial_usergroup_number = serializers.IntegerField(
        required=False,
        default=6000,
        help_text="GLAuth initial usergroup number",
        min_value=0,
    )
    username_anonymized_prefix = serializers.CharField(
        required=False,
        default="waldur_",
        help_text="GLAuth prefix for anonymized usernames",
    )
    username_generation_policy = serializers.ChoiceField(
        required=False,
        choices=[option.value for option in UsernameGenerationPolicy],
        help_text="GLAuth username generation policy",
        default=UsernameGenerationPolicy.SERVICE_PROVIDER.value,
    )


class RancherPluginOptionsSerializer(serializers.Serializer):
    flavors_regex = serializers.CharField(
        required=False, help_text="Regular expression to limit flavors list"
    )


class ManagedRancherPluginOptionsSerializer(serializers.Serializer):
    openstack_offering_uuid_list = serializers.ListSerializer(
        child=serializers.CharField(validators=[core_utils.validate_uuid]),
        required=False,
        help_text="List of UUID of OpenStack offerings where tenant can be created",
    )
    managed_rancher_server_flavor_name = serializers.CharField(required=False)
    managed_rancher_server_system_volume_size_gb = serializers.IntegerField(
        required=False
    )
    managed_rancher_server_system_volume_type_name = serializers.CharField(
        required=False
    )
    managed_rancher_server_data_volume_size_gb = serializers.IntegerField(
        required=False
    )
    managed_rancher_server_data_volume_type_name = serializers.CharField(required=False)
    managed_rancher_worker_system_volume_size_gb = serializers.IntegerField(
        required=False
    )
    managed_rancher_worker_system_volume_type_name = serializers.CharField(
        required=False
    )
    managed_rancher_load_balancer_cloud_init_template = serializers.CharField(
        required=False,
        allow_blank=True,
    )
    managed_rancher_load_balancer_flavor_name = serializers.CharField(required=False)
    managed_rancher_load_balancer_system_volume_size_gb = serializers.IntegerField(
        required=False
    )
    managed_rancher_load_balancer_system_volume_type_name = serializers.CharField(
        required=False
    )
    managed_rancher_load_balancer_data_volume_size_gb = serializers.IntegerField(
        required=False
    )
    managed_rancher_load_balancer_data_volume_type_name = serializers.CharField(
        required=False
    )


class AgentPluginOptionsSerializer(serializers.Serializer):
    account_name_generation_policy = serializers.ChoiceField(
        required=False,
        choices=[None, "project_slug"],
        help_text="Slurm account name generation policy",
        default=None,
        allow_null=True,
    )


class MergedPluginOptionsSerializer(
    LifecyclePluginOptionsSerializer,
    OpenStackPluginOptionsSerializer,
    HeappePluginOptionsSerializer,
    GLAuthPluginOptionsSerializer,
    SupportPluginOptionsSerializer,
    RancherPluginOptionsSerializer,
    ManagedRancherPluginOptionsSerializer,
    AgentPluginOptionsSerializer,
):
    pass


class HeappeSecretOptionsSerializer(serializers.Serializer):
    heappe_cluster_password = serializers.CharField(
        required=False, help_text="HEAppE cluster password"
    )
    heappe_password = serializers.CharField(required=False, help_text="HEAppE password")


class IPMappingSerializer(serializers.Serializer):
    floating_ip = serializers.CharField(help_text="Floating IP")
    external_ip = serializers.CharField(help_text="External IP")


class OpenstackSecretOptionsSerializer(serializers.Serializer):
    ipv4_external_ip_mapping = IPMappingSerializer(
        required=False,
        help_text="OpenStack IPv4 external IP mapping",
        many=True,
    )
    openstack_api_tls_certificate = serializers.CharField(
        allow_blank=True,
        required=False,
        validators=[core_validators.validate_x509_certificate],
    )
    dns_nameservers = serializers.ListField(
        child=serializers.CharField(),
        help_text=_(
            "Default value for new subnets DNS name servers. Should be defined as list."
        ),
        required=False,
    )


class GLAuthSecretOptionsSerializer(serializers.Serializer):
    shared_user_password = serializers.CharField(
        required=False, help_text="GLAuth shared user password"
    )


class SupportSecretOptionsSerializer(serializers.Serializer):
    template_confirmation_comment = serializers.CharField(
        required=False, help_text="Template confirmation comment"
    )


class ScriptSecretOptionsSerializer(serializers.Serializer):
    language = serializers.CharField(
        required=False, help_text="Script language: Python or Bash"
    )
    environ = serializers.JSONField(
        required=False, help_text="Script environment variables"
    )
    create = serializers.CharField(
        required=False, help_text="Script for resource creation"
    )
    terminate = serializers.CharField(
        required=False, help_text="Script for resource termination"
    )
    update = serializers.CharField(
        required=False, help_text="Script for resource update"
    )
    pull = serializers.CharField(
        required=False, help_text="Script for regular resource pull"
    )


class RemoteServiceSecretOptionsSerializer(serializers.Serializer):
    api_url = serializers.CharField(required=False, help_text="API URL")
    token = serializers.CharField(required=False, help_text="Waldur access token")
    customer_uuid = serializers.CharField(
        validators=[core_utils.validate_uuid],
        required=False,
        help_text="Organization UUID",
    )


class ManagedRancherSecretOptionsSerializer(serializers.Serializer):
    backend_url = serializers.CharField(
        max_length=200,
        label=_("Rancher server URL"),
        validators=[BackendURLValidator],
        required=False,
    )

    username = serializers.CharField(
        max_length=100, label=_("Rancher access key"), required=False
    )

    password = serializers.CharField(
        max_length=100,
        label=_("Rancher secret key"),
        required=False,
    )

    customer_uuid = serializers.CharField(
        validators=[core_utils.validate_uuid],
        required=False,
        help_text="UUID of organization where project can be created",
    )

    cloud_init_template = serializers.CharField(required=False)

    vault_host = serializers.CharField(
        help_text=_("Host of the Vault server"),
        required=False,
    )

    vault_port = serializers.IntegerField(
        help_text=_("Port of the Vault server"),
        required=False,
    )
    vault_token = serializers.CharField(
        help_text=_("Token for the Vault server"),
        required=False,
    )
    vault_tls_verify = serializers.BooleanField(
        help_text=_("Whether to verify the Vault server certificate"),
        required=False,
    )

    keycloak_url = serializers.CharField(
        help_text=_("URL of the Keycloak server"),
        required=False,
    )

    keycloak_realm = serializers.CharField(
        help_text=_("Keycloak realm for Rancher"),
        required=False,
    )

    keycloak_user_realm = serializers.CharField(
        help_text=_("Keycloak user realm for auth"),
        required=False,
    )

    keycloak_username = serializers.CharField(
        help_text=_("Username of the Keycloak integration user"),
        required=False,
    )

    keycloak_password = serializers.CharField(
        help_text=_("Password of the Keycloak integration user"),
        required=False,
    )

    keycloak_sync_frequency = serializers.IntegerField(
        help_text=_("Frequency in minutes for syncing Keycloak users"),
        required=False,
    )

    keycloak_ssl_verify = serializers.BooleanField(
        help_text=_("Indicates whether verify SSL certificates"),
        required=False,
    )

    argocd_k8s_namespace = serializers.CharField(
        help_text=_("Namespace where ArgoCD is deployed"),
        required=False,
    )

    argocd_k8s_kubeconfig = serializers.CharField(
        help_text=_("Kubeconfig with access to namespace where ArgoCD is deployed"),
        required=False,
    )

    base_image_name = serializers.CharField(
        help_text=_("Base image name"),
        required=False,
    )

    private_registry_url = serializers.CharField(
        help_text=_("URL of a private registry for a cluster"),
        required=False,
    )

    private_registry_user = serializers.CharField(
        help_text=_("Username for accessing a private registry"),
        required=False,
    )

    private_registry_password = serializers.CharField(
        help_text=_("Password for accessing a private registry"),
        required=False,
    )

    k8s_version = serializers.CharField(
        help_text=_("Kubernetes version"),
        required=False,
    )

    node_disk_driver = serializers.ChoiceField(
        required=False,
        help_text=_("OpenStack disk driver for Rancher nodes"),
        choices=["sd", "vd"],
    )


class GenericSecretOptionsSerializer(serializers.Serializer):
    pass


class MergedSecretOptionsSerializer(
    HeappeSecretOptionsSerializer,
    OpenstackSecretOptionsSerializer,
    GLAuthSecretOptionsSerializer,
    SupportSecretOptionsSerializer,
    ScriptSecretOptionsSerializer,
    GenericSecretOptionsSerializer,
    RemoteServiceSecretOptionsSerializer,
    ManagedRancherSecretOptionsSerializer,
):
    pass


@extend_schema_field(MergedPluginOptionsSerializer)
class MergedPluginOptionsField(serializers.JSONField):
    pass


@extend_schema_field(MergedSecretOptionsSerializer)
class MergedSecretOptionsField(serializers.JSONField):
    pass


class ReportSectionSerializer(serializers.Serializer):
    header = serializers.CharField()
    body = serializers.CharField()


class ResourceReportSerializer(serializers.Serializer):
    report = ReportSectionSerializer(many=True)

    def validate_report(self, report):
        if len(report) == 0:
            raise serializers.ValidationError(
                "Report object should contain at least one section."
            )

        return report


@extend_schema_field(serializers.ListField(child=ReportSectionSerializer()))
class ResourceReportField(serializers.JSONField):
    pass


class ServiceProviderSerializer(
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.ServiceProvider
        fields = (
            "url",
            "uuid",
            "created",
            "description",
            "enable_notifications",
            "customer",
            "customer_name",
            "customer_uuid",
            "customer_image",
            "customer_abbreviation",
            "customer_slug",
            "customer_native_name",
            "customer_country",
            "image",
            "organization_groups",
            "description",
            "offering_count",
        )
        related_paths = {
            "customer": ("uuid", "name", "native_name", "abbreviation", "slug")
        }
        protected_fields = ("customer",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-service-provider-detail",
            },
            "customer": {"lookup_field": "uuid"},
        }

    customer_image = serializers.ImageField(source="customer.image", read_only=True)
    customer_country = serializers.CharField(source="customer.country", read_only=True)
    organization_groups = structure_serializers.OrganizationGroupSerializer(
        many=True, read_only=True
    )

    def get_fields(self):
        fields = super().get_fields()
        if self.context["request"].user.is_anonymous:
            del fields["enable_notifications"]
        return fields

    def validate(self, attrs):
        if not self.instance:
            permissions.can_register_service_provider(
                self.context["request"], attrs["customer"]
            )
        return attrs


class ServiceProviderApiSecretCodeSerializer(serializers.Serializer):
    api_secret_code = serializers.CharField(read_only=True)


class SetOfferingsUsernameSerializer(serializers.Serializer):
    user_uuid = serializers.UUIDField()
    username = serializers.CharField()


class NestedAttributeOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.AttributeOption
        fields = ("key", "title")


class NestedAttributeSerializer(serializers.ModelSerializer):
    options = NestedAttributeOptionSerializer(many=True)

    class Meta:
        model = models.Attribute
        fields = ("key", "title", "type", "options", "required", "default")


class NestedSectionSerializer(serializers.ModelSerializer):
    attributes = NestedAttributeSerializer(many=True, read_only=True)

    class Meta:
        model = models.Section
        fields = ("key", "title", "attributes", "is_standalone")


class NestedColumnSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.CategoryColumn
        fields = ("uuid", "index", "title", "attribute", "widget")


class CategoryColumnSerializer(NestedColumnSerializer):
    category = serializers.HyperlinkedRelatedField(
        queryset=models.Category.objects.all(),
        view_name="marketplace-category-detail",
        lookup_field="uuid",
    )

    class Meta(NestedColumnSerializer.Meta):
        fields = NestedColumnSerializer.Meta.fields + ("category",)


class CategoryComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.CategoryComponent
        fields = ("type", "name", "description", "measured_unit")


class CategoryHelpArticleSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.CategoryHelpArticle
        fields = ("title", "url")


class CategorySerializerForForNestedFields(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Category
        fields = ("url", "uuid", "title")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-category-detail",
            },
        }


class CategoryHelpArticlesSerializer(serializers.ModelSerializer):
    categories = CategorySerializerForForNestedFields(many=True)

    class Meta:
        model = models.CategoryHelpArticle
        fields = ("title", "url", "categories")

    def create(self, validated_data):
        categories = validated_data.pop("categories")
        article = models.CategoryHelpArticle.objects.create(**validated_data)
        for category in categories:
            category = models.Category.objects.get(**category)
            article.categories.add(category)
        return article

    def update(self, instance, validated_data):
        categories = validated_data.pop("categories")
        article = super().update(instance, validated_data)
        instance.categories.clear()
        for category in categories:
            category = models.Category.objects.get(**category)
            instance.categories.add(category)
        return article


class CategoryComponentsSerializer(serializers.ModelSerializer):
    category = CategorySerializerForForNestedFields()

    class Meta:
        model = models.CategoryComponent
        fields = ("uuid", "type", "name", "description", "measured_unit", "category")

    def create(self, validated_data):
        category = validated_data.pop("category")
        category = models.Category.objects.get(**category)
        validated_data["category"] = category
        return super().create(validated_data)

    def update(self, instance, validated_data):
        category = validated_data.pop("category")
        category = models.Category.objects.get(**category)
        validated_data["category"] = category
        return super().update(instance, validated_data)


class CategoryGroupSerializer(
    core_serializers.AugmentedSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.CategoryGroup
        fields = (
            "url",
            "uuid",
            "title",
            "description",
            "icon",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-category-group-detail",
            },
        }


class MarketplaceCategorySerializer(
    core_serializers.AugmentedSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    offering_count = serializers.SerializerMethodField()
    available_offerings_count = serializers.IntegerField(read_only=True)
    sections = NestedSectionSerializer(many=True, read_only=True)
    columns = NestedColumnSerializer(many=True, read_only=True)
    components = CategoryComponentSerializer(many=True, read_only=True)
    articles = CategoryHelpArticleSerializer(many=True, read_only=True)

    @staticmethod
    def eager_load(queryset, request):
        return queryset.distinct().prefetch_related("sections", "sections__attributes")

    def get_offering_count(self, category) -> int:
        request = self.context["request"]
        customer_uuid = request.GET.get("customer_uuid")
        shared = request.GET.get("shared")

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

        allowed_customer_uuid = request.query_params.get("allowed_customer_uuid")
        if allowed_customer_uuid and core_utils.is_uuid_like(allowed_customer_uuid):
            offerings = offerings.filter_for_customer(allowed_customer_uuid)

        project_uuid = request.query_params.get("project_uuid")
        if project_uuid and core_utils.is_uuid_like(project_uuid):
            offerings = offerings.filter_for_project(project_uuid)

        offering_name = request.query_params.get("offering_name")
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
            "url",
            "uuid",
            "title",
            "description",
            "icon",
            "default_vm_category",
            "default_volume_category",
            "default_tenant_category",
            "offering_count",
            "available_offerings_count",
            "sections",
            "columns",
            "components",
            "articles",
            "group",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "marketplace-category-detail"},
            "group": {
                "lookup_field": "uuid",
                "view_name": "marketplace-category-group-detail",
            },
        }

    def validate(self, data):
        data = super().validate(data)

        for flag in [
            "default_volume_category",
            "default_vm_category",
            "default_tenant_category",
        ]:
            if data.get(flag):
                category_exists = (
                    models.Category.objects.filter(**{flag: True})
                    .exclude(id=self.instance.id if self.instance else None)
                    .exists()
                )
                if category_exists:
                    raise serializers.ValidationError(
                        {
                            flag: _("A category with {} as {} already exists.").format(
                                flag.replace("_", " "), data[flag]
                            ),
                        }
                    )

        return data


PriceSerializer = serializers.DecimalField(
    min_value=Decimal(0),
    max_digits=common_mixins.PRICE_MAX_DIGITS,
    decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
)


def validate_components(
    new_keys: set[str], valid_keys: set[str], plan: models.Plan
) -> dict[str, models.PlanComponent]:
    invalid_components = ", ".join(sorted(new_keys - valid_keys))
    if invalid_components:
        raise serializers.ValidationError(
            _("Invalid components %s.") % invalid_components
        )

    old_keys = set(plan.components.values_list("component__type", flat=True))
    for key in new_keys - old_keys:
        component = plan.offering.components.get(type=key)
        models.PlanComponent.objects.create(plan=plan, component=component)

    return {component.component.type: component for component in plan.components.all()}


class PricesUpdateSerializer(serializers.Serializer):
    prices = serializers.DictField(child=PriceSerializer)

    def save(self):
        plan: models.Plan = self.instance
        future_prices = self.validated_data["prices"]
        new_keys = set(future_prices.keys())
        valid_types = {component.type for component in plan.offering.components.all()}
        component_map = validate_components(new_keys, valid_types, plan)
        if models.Resource.objects.filter(plan=plan).exists():
            price_field = "future_price"
        else:
            price_field = "price"
        for key, old_component in component_map.items():
            new_price = future_prices.get(key, 0)
            old_price = getattr(old_component, price_field) or 0
            if not prices_are_equal(old_price, new_price):
                setattr(old_component, price_field, new_price)
                old_component.save(update_fields=[price_field])


class QuotasUpdateSerializer(serializers.Serializer):
    quotas = serializers.DictField(child=serializers.IntegerField(min_value=0))

    def save(self):
        new_quotas = self.validated_data["quotas"]
        new_keys = set(new_quotas.keys())
        plan: models.Plan = self.instance

        valid_types = {
            component.type
            for component in plan.offering.components.all()
            if component.billing_type == models.OfferingComponent.BillingTypes.FIXED
        }
        component_map = validate_components(new_keys, valid_types, plan)
        for key, old_component in component_map.items():
            new_amount = new_quotas.get(key, 0)
            if old_component.amount != new_amount:
                old_component.amount = new_amount
                old_component.save(update_fields=["amount"])


class BasePlanSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    organization_groups = structure_serializers.OrganizationGroupSerializer(
        many=True, read_only=True
    )
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = models.Plan
        fields = (
            "url",
            "uuid",
            "name",
            "description",
            "article_code",
            "max_amount",
            "archived",
            "is_active",
            "unit_price",
            "unit",
            "init_price",
            "switch_price",
            "backend_id",
            "organization_groups",
        )
        read_ony_fields = ("unit_price", "archived")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

    def get_fields(self):
        fields = super().get_fields()
        method = self.context["view"].request.method
        fields["prices"] = serializers.SerializerMethodField()
        fields["future_prices"] = serializers.SerializerMethodField()
        fields["quotas"] = serializers.SerializerMethodField()
        fields["resources_count"] = serializers.SerializerMethodField()
        if method == "GET":
            fields["plan_type"] = serializers.SerializerMethodField()
            fields["minimal_price"] = serializers.SerializerMethodField()
        return fields

    def get_is_active(self, plan: models.Plan) -> bool:
        return plan.is_active

    def get_prices(self, plan: models.Plan) -> dict[str, float]:
        return {item.component.type: item.price for item in plan.components.all()}

    def get_future_prices(self, plan: models.Plan) -> dict[str, float]:
        return {
            item.component.type: item.future_price for item in plan.components.all()
        }

    def get_quotas(self, plan: models.Plan) -> dict[str, float]:
        return {item.component.type: item.amount for item in plan.components.all()}

    def get_resources_count(self, plan: models.Plan) -> int:
        return models.Resource.objects.filter(plan=plan).count()

    def get_plan_type(self, plan: models.Plan) -> str:
        plan_type = None
        components_types = set()

        for plan_component in plan.components.all():
            offering_component = plan_component.component

            if plan_component.price:
                components_types.add(offering_component.billing_type)

        if len(components_types) == 1:
            if models.OfferingComponent.BillingTypes.USAGE in components_types:
                plan_type = "usage-based"
            if models.OfferingComponent.BillingTypes.FIXED in components_types:
                plan_type = "fixed"
            if models.OfferingComponent.BillingTypes.ONE_TIME in components_types:
                plan_type = "one-time"
            if models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH in components_types:
                plan_type = "on-plan-switch"
            if models.OfferingComponent.BillingTypes.LIMIT in components_types:
                plan_type = "limit"
        elif len(components_types) > 1:
            plan_type = "mixed"

        return plan_type

    def get_minimal_price(self, plan: models.Plan) -> float:
        price = 0

        components: QuerySet[models.PlanComponent] = plan.components.all()

        for plan_component in components:
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

    url = PublicPlanField(
        lookup_field="uuid",
        lookup_url_kwarg="plan_uuid",
        view_name="marketplace-public-offering-plan-detail",
        queryset=models.Plan.objects.all(),
    )


class BaseProviderPlanSerializer(BasePlanSerializer):
    """Serializer to display the provider's plan without offering info."""

    class Meta(BasePlanSerializer.Meta):
        view_name = "marketplace-plan-detail"


class ProviderPlanDetailsSerializer(BaseProviderPlanSerializer):
    """Serializer to display the provider's plan in the REST API."""

    class Meta(BaseProviderPlanSerializer.Meta):
        fields = BaseProviderPlanSerializer.Meta.fields + ("offering",)
        protected_fields = ("offering",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        }

    def validate(self, attrs):
        if not self.instance:
            if not has_permission(
                self.context["request"],
                PermissionEnum.CREATE_OFFERING_PLAN,
                attrs["offering"].customer,
            ):
                raise PermissionDenied()
        return attrs

    def create(self, validated_data):
        if self.instance:
            offering = self.instance.offering
        else:
            offering = validated_data.pop("offering")
        return create_plan(offering, validated_data)

    def update(self, instance, validated_data):
        update_plan_details(instance, validated_data)
        return instance


class PlanUsageRequestSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(required=False)
    customer_provider_uuid = serializers.UUIDField(required=False)
    o = serializers.ChoiceField(
        choices=(
            "usage",
            "limit",
            "remaining",
            "-usage",
            "-limit",
            "-remaining",
        ),
        required=False,
    )


class PlanUsageResponseSerializer(serializers.Serializer):
    plan_uuid = serializers.UUIDField(read_only=True, source="uuid")
    plan_name = serializers.CharField(read_only=True, source="name")

    limit = serializers.IntegerField(read_only=True)
    usage = serializers.IntegerField(read_only=True)
    remaining = serializers.IntegerField(read_only=True)

    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.CharField(read_only=True, source="offering.name")

    customer_provider_uuid = serializers.UUIDField(
        read_only=True, source="offering.customer.uuid"
    )
    customer_provider_name = serializers.CharField(
        read_only=True, source="offering.customer.name"
    )


class NestedScreenshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Screenshot
        fields = ("name", "uuid", "description", "image", "thumbnail", "created")


class NestedOfferingFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.OfferingFile
        fields = (
            "name",
            "created",
            "file",
        )


class ScreenshotSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.Screenshot
        fields = (
            "url",
            "uuid",
            "name",
            "created",
            "description",
            "image",
            "thumbnail",
            "offering",
            "customer_uuid",
        )
        protected_fields = ("offering", "image")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        }

    customer_uuid = serializers.UUIDField(
        read_only=True, source="offering.customer.uuid"
    )

    def validate(self, attrs):
        if self.instance:
            permission = PermissionEnum.UPDATE_OFFERING_SCREENSHOT
            customer = self.instance.offering.customer
        else:
            permission = PermissionEnum.CREATE_OFFERING_SCREENSHOT
            customer = attrs["offering"].customer

        if not has_permission(
            self.context["request"],
            permission,
            customer,
        ):
            raise PermissionDenied()
        return attrs


FIELD_TYPES = (
    "boolean",
    "integer",
    "money",
    "string",
    "text",
    "html_text",
    "select_string",
    "select_string_multi",
    "select_openstack_tenant",
    "select_multiple_openstack_tenants",
    "select_openstack_instance",
    "select_multiple_openstack_instances",
    "date",
    "time",
)


class OptionFieldSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=FIELD_TYPES)
    label = serializers.CharField()
    help_text = serializers.CharField(required=False)
    required = serializers.BooleanField(default=False)
    choices = serializers.ListField(child=serializers.CharField(), required=False)
    default = serializers.CharField(required=False)
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
            "uuid",
            "billing_type",
            "type",
            "name",
            "description",
            "measured_unit",
            "unit_factor",
            "limit_period",
            "limit_amount",
            "article_code",
            "max_value",
            "min_value",
            "max_available_limit",
            "is_boolean",
            "default_limit",
            "factor",
            "is_builtin",
        )
        extra_kwargs = {
            "billing_type": {"required": True},
        }

    def validate(self, attrs):
        if attrs.get("is_boolean"):
            attrs["min_value"] = 0
            attrs["max_value"] = 1
            attrs["limit_period"] = models.OfferingComponent.LimitPeriods.MONTH
            attrs["limit_amount"] = None
        if self.instance and self.instance.offering.type == TENANT_TYPE:
            protected_fields = set(attrs.keys()) & {
                "type",
                "name",
                "measured_unit",
                "billing_type",
            }
            if protected_fields:
                raise serializers.ValidationError(
                    "OpenStack offering components are not editable."
                )
        return attrs

    def create(self, validated_data):
        offering = validated_data.get("offering")

        if offering is not None:
            offering_type = validated_data["offering"].type
            component_type = validated_data["type"]

            is_builtin = component_type in [
                c.type for c in plugins.manager.get_components(offering_type)
            ]

            if is_builtin:
                raise serializers.ValidationError(
                    _("Cannot create a component of built-in type: %s" % component_type)
                )

            if offering.components.filter(type=component_type).exists():
                raise serializers.ValidationError(
                    _("Component %s already exists." % component_type)
                )

        return super().create(validated_data)

    def get_factor(self, offering_component: models.OfferingComponent) -> int:
        builtin_components = plugins.manager.get_components(
            offering_component.offering.type
        )
        for c in builtin_components:
            if c.type == offering_component.type:
                return c.factor


class ExportImportOfferingComponentSerializer(OfferingComponentSerializer):
    offering_id = serializers.IntegerField(write_only=True, required=False)

    class Meta(OfferingComponentSerializer.Meta):
        fields = OfferingComponentSerializer.Meta.fields + ("offering_id",)


class ExportImportPlanComponentSerializer(serializers.ModelSerializer):
    component = ExportImportOfferingComponentSerializer(required=False)
    component_id = serializers.IntegerField(write_only=True, required=False)
    plan_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = models.PlanComponent
        fields = (
            "amount",
            "price",
            "future_price",
            "component",
            "component_id",
            "plan_id",
        )


class ExportImportPlanSerializer(serializers.ModelSerializer):
    """Serializer for export and import of plan from/to an exported offering.
    This serializer differs from PlanDetailsSerializer in methods and fields."""

    components = ExportImportPlanComponentSerializer(many=True)
    offering_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = models.Plan
        fields = (
            "name",
            "description",
            "article_code",
            "max_amount",
            "archived",
            "is_active",
            "unit_price",
            "unit",
            "init_price",
            "switch_price",
            "components",
            "offering_id",
        )

    def save(self, **kwargs):
        validated_data = self.validated_data
        components = validated_data.pop("components", [])
        plan = super().save(**kwargs)

        offering_components = []

        for component in components:
            serialized_offering_component = component.get("component")

            if serialized_offering_component:
                offering_component = plan.offering.components.get(
                    type=serialized_offering_component["type"]
                )
                offering_components.append(offering_component)

        plan.components.exclude(component__in=offering_components).delete()

        for component in components:
            component["plan_id"] = plan.id
            serialized_offering_component = component.pop("component")

            if serialized_offering_component:
                offering_component = plan.offering.components.get(
                    type=serialized_offering_component["type"]
                )
                component["component_id"] = offering_component.id
                offering_components.append(offering_component)

                if plan.components.filter(
                    component_id=component["component_id"]
                ).exists():
                    existed_component = plan.components.get(
                        component_id=component["component_id"]
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
    plugin_options = MergedPluginOptionsSerializer(required=False)
    secret_options = MergedSecretOptionsSerializer(required=False)

    class Meta:
        model = models.Offering
        fields = (
            "name",
            "description",
            "full_description",
            "terms_of_service",
            "access_url",
            "attributes",
            "options",
            "components",
            "plugin_options",
            "secret_options",
            "state",
            "vendor_details",
            "getting_started",
            "integration_guide",
            "type",
            "shared",
            "billable",
            "category_id",
            "customer_id",
            "plans",
            "latitude",
            "longitude",
        )

    def save(self, **kwargs):
        validated_data = self.validated_data
        components = validated_data.pop("components", [])
        plans = validated_data.pop("plans", [])
        offering = super().save(**kwargs)

        component_types = []

        for component in components:
            component["offering_id"] = offering.id
            component_types.append(component["type"])

            if offering.components.filter(type=component["type"]).exists():
                existed_component = offering.components.get(type=component["type"])
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
            plan["offering_id"] = offering.id
            plan_names.append(plan["name"])

            if offering.plans.filter(name=plan["name"]).exists():
                existed_plan = offering.plans.get(name=plan["name"])
                plan_serializer = ExportImportPlanSerializer(existed_plan, data=plan)
            else:
                plan_serializer = ExportImportPlanSerializer(data=plan)

            plan_serializer.is_valid(raise_exception=True)
            plan_serializer.save()

        offering.plans.exclude(name__in=plan_names).delete()

        return offering


class PlanComponentSerializer(serializers.ModelSerializer):
    offering_name = serializers.ReadOnlyField(source="plan.offering.name")
    plan_name = serializers.ReadOnlyField(source="plan.name")
    plan_unit = serializers.ReadOnlyField(source="plan.unit")
    component_name = serializers.ReadOnlyField(source="component.name")
    measured_unit = serializers.ReadOnlyField(source="component.measured_unit")
    billing_type = serializers.ReadOnlyField(source="component.billing_type")

    class Meta:
        model = models.PlanComponent
        fields = (
            "offering_name",
            "plan_name",
            "plan_unit",
            "component_name",
            "measured_unit",
            "billing_type",
            "amount",
            "price",
            "future_price",
        )


class NestedEndpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.OfferingAccessEndpoint
        fields = ("uuid", "name", "url")

    url = serializers.CharField(validators=[core_validators.BackendURLValidator])


class EndpointUUIDSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()


class NestedRoleSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.OfferingUserRole
        fields = ("uuid", "name", "url")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-offering-user-role-detail",
            },
        }


class OfferingBackendMetadataSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Offering
        fields = ("backend_metadata",)


@extend_schema_field(OfferingOptionsSerializer)
class OfferingOptionsField(serializers.JSONField):
    pass


class ProviderOfferingDetailsSerializer(
    core_serializers.SlugSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    structure_serializers.CountrySerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    attributes = serializers.JSONField(required=False)
    options = OfferingOptionsField(read_only=True)
    resource_options = OfferingOptionsField(read_only=True)
    plugin_options = MergedPluginOptionsField(read_only=True)
    secret_options = MergedSecretOptionsField(read_only=True)
    service_attributes = serializers.SerializerMethodField()
    components = OfferingComponentSerializer(required=False, many=True)
    order_count = serializers.SerializerMethodField()
    plans = BaseProviderPlanSerializer(many=True, required=False)
    screenshots = NestedScreenshotSerializer(many=True, read_only=True)
    state = serializers.SerializerMethodField()
    scope = core_serializers.GenericRelatedField(read_only=True)
    scope_uuid = serializers.UUIDField(
        read_only=True, source="scope.uuid", allow_null=True
    )
    scope_name = serializers.UUIDField(
        read_only=True, source="scope.name", allow_null=True
    )
    scope_state = serializers.SerializerMethodField()
    scope_error_message = serializers.SerializerMethodField()
    files = NestedOfferingFileSerializer(many=True, read_only=True)
    quotas = serializers.SerializerMethodField()
    organization_groups = structure_serializers.OrganizationGroupSerializer(
        many=True, read_only=True
    )
    total_customers = serializers.SerializerMethodField()
    total_cost = serializers.SerializerMethodField()
    total_cost_estimated = serializers.SerializerMethodField()
    endpoints = NestedEndpointSerializer(many=True, read_only=True)
    roles = NestedRoleSerializer(many=True, read_only=True)

    class Meta:
        model = models.Offering
        fields = (
            "url",
            "uuid",
            "created",
            "name",
            "slug",
            "description",
            "full_description",
            "terms_of_service",
            "terms_of_service_link",
            "privacy_policy_link",
            "access_url",
            "endpoints",
            "roles",
            "customer",
            "customer_uuid",
            "customer_name",
            "project",
            "project_uuid",
            "project_name",
            "category",
            "category_uuid",
            "category_title",
            "attributes",
            "options",
            "resource_options",
            "components",
            "plugin_options",
            "secret_options",
            "service_attributes",
            "state",
            "vendor_details",
            "getting_started",
            "integration_guide",
            "thumbnail",
            "order_count",
            "plans",
            "screenshots",
            "type",
            "shared",
            "billable",
            "scope",
            "scope_uuid",
            "scope_name",
            "scope_state",
            "scope_error_message",
            "files",
            "quotas",
            "paused_reason",
            "datacite_doi",
            "citation_count",
            "latitude",
            "longitude",
            "country",
            "backend_id",
            "organization_groups",
            "image",
            "total_customers",
            "total_cost",
            "total_cost_estimated",
            "parent_description",
            "parent_uuid",
            "parent_name",
            "backend_metadata",
        )
        related_paths = {
            "customer": ("uuid", "name"),
            "project": ("uuid", "name"),
            "category": ("uuid", "title"),
            "parent": (
                "uuid",
                "description",
                "name",
            ),
        }
        protected_fields = ("customer", "type")
        read_only_fields = (
            "state",
            "paused_reason",
            "citation_count",
            "project",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
            "customer": {
                "lookup_field": "uuid",
                "view_name": "customer-detail",
                "allow_null": True,
            },
            "customer_name": {"allow_null": True},
            "customer_uuid": {"allow_null": True},
            "project": {
                "lookup_field": "uuid",
                "view_name": "project-detail",
                "allow_null": True,
            },
            "project_name": {"allow_null": True},
            "project_uuid": {"allow_null": True},
            "parent_name": {"allow_null": True},
            "parent_uuid": {"allow_null": True},
            "parent_description": {"allow_null": True},
            "category": {
                "lookup_field": "uuid",
                "view_name": "marketplace-category-detail",
            },
        }
        view_name = "marketplace-provider-offering-detail"

    def get_fields(self):
        fields = super().get_fields()
        if self.instance and not self.can_see_secret_options():
            if "secret_options" in fields:
                fields.pop("secret_options")
            if "service_attributes" in fields:
                fields.pop("service_attributes")
        method = self.context["view"].request.method
        if method == "GET":
            if "components" in fields:
                fields["components"] = serializers.SerializerMethodField(
                    "get_components"
                )
            if "plans" in fields:
                fields["plans"] = serializers.SerializerMethodField(
                    "get_filtered_plans"
                )
            if "attributes" in fields:
                fields["attributes"] = serializers.SerializerMethodField(
                    "get_attributes"
                )
            if (
                "plugin_options" in fields
                and isinstance(self.instance, models.Offering)
                and self.instance.parent
            ):
                fields["plugin_options"] = MergedPluginOptionsField(
                    source="parent.plugin_options", read_only=True
                )

        return fields

    def can_see_secret_options(self) -> bool:
        request = self.context.get("request")
        return request and permissions.can_see_secret_options(request, self.instance)

    def get_total_customers(self, offering: models.Offering) -> int | None:
        # Added via annotate in ProviderOfferingViewSet.get_queryset
        try:
            return offering.total_customers
        except AttributeError:
            return None

    def get_total_cost(self, offering: models.Offering) -> int | None:
        # Added via annotate in ProviderOfferingViewSet.get_queryset
        try:
            return offering.total_cost
        except AttributeError:
            return None

    def get_total_cost_estimated(self, offering: models.Offering) -> int | None:
        # Added via annotate in ProviderOfferingViewSet.get_queryset
        try:
            return offering.total_cost_estimated
        except AttributeError:
            return None

    def get_state(
        self, offering: models.Offering
    ) -> Literal["Draft", "Active", "Paused", "Archived"]:
        return offering.get_state_display()

    def get_scope_state(self, offering: models.Offering) -> CoreStateType | None:
        try:
            return offering.scope.get_state_display()
        except AttributeError:
            return None

    def get_scope_error_message(self, offering: models.Offering) -> str | None:
        try:
            return offering.scope.error_message
        except AttributeError:
            return None

    @extend_schema_field(QuotaSerializer(many=True))
    def get_quotas(self, offering: models.Offering):
        try:
            return offering.scope.quotas
        except AttributeError:
            return []

    def get_order_count(self, offering: models.Offering) -> int:
        try:
            return offering.get_quota_usage("order_count")
        except ObjectDoesNotExist:
            return 0

    @extend_schema_field(OfferingComponentSerializer(many=True))
    def get_components(self, offering: models.Offering):
        qs = (offering.parent or offering).components
        func = manager.get_components_filter(offering.type)
        if func:
            qs = func(offering, qs)
        return OfferingComponentSerializer(qs, many=True, context=self.context).data

    @extend_schema_field(BaseProviderPlanSerializer(many=True))
    def get_filtered_plans(self, offering: models.Offering):
        customer_uuid = self.context["request"].GET.get("allowed_customer_uuid")
        user = self.context["request"].user
        qs = utils.get_plans_available_for_user(
            user=user, offering=offering, allowed_customer_uuid=customer_uuid
        )
        return BaseProviderPlanSerializer(qs, many=True, context=self.context).data

    @extend_schema_field(dict)
    def get_attributes(self, offering: models.Offering) -> dict[str, any]:
        func = manager.get_change_attributes_for_view(offering.type)

        if func:
            return func(offering.attributes)

        return offering.attributes

    @extend_schema_field(dict)
    def get_service_attributes(self, offering: models.Offering) -> dict[str, any]:
        try:
            service = offering.scope
        except AttributeError:
            return {}
        if isinstance(service, structure_models.BaseResource):
            service = service.service_settings
        if isinstance(service, models.Offering):
            return {}
        if not service:
            return {}
        return {
            "backend_url": service.backend_url,
            "username": service.username,
            "password": service.password,
            "domain": service.domain,
            "token": service.token,
            **service.options,
        }


class PublicOfferingDetailsSerializer(ProviderOfferingDetailsSerializer):
    class Meta(ProviderOfferingDetailsSerializer.Meta):
        view_name = "marketplace-public-offering-detail"

    plugin_options = MergedPluginOptionsField(read_only=True)

    @extend_schema_field(BasePublicPlanSerializer(many=True))
    def get_filtered_plans(self, offering: models.Offering):
        customer_uuid = self.context["request"].GET.get("allowed_customer_uuid")
        user = self.context["request"].user
        qs = utils.get_plans_available_for_user(
            user=user, offering=offering, allowed_customer_uuid=customer_uuid
        )
        return BasePublicPlanSerializer(qs, many=True, context=self.context).data

    def get_fields(self):
        fields = super().get_fields()
        if "secret_options" in fields:
            fields.pop("secret_options")
        if "service_attributes" in fields:
            fields.pop("service_attributes")
        return fields


class OfferingComponentLimitSerializer(serializers.Serializer):
    min = serializers.IntegerField(min_value=0)
    max = serializers.IntegerField(min_value=0)
    max_available_limit = serializers.IntegerField(min_value=0)


def create_plan(offering, plan_data):
    components = {component.type: component for component in offering.components.all()}

    plan = models.Plan.objects.create(offering=offering, **plan_data)

    for name, component in components.items():
        models.PlanComponent.objects.create(
            plan=plan,
            component=component,
        )
    return plan


class OfferingCreateSerializer(ProviderOfferingDetailsSerializer):
    class Meta(ProviderOfferingDetailsSerializer.Meta):
        model = models.Offering
        fields = ProviderOfferingDetailsSerializer.Meta.fields + ("limits",)

    limits = serializers.DictField(
        child=OfferingComponentLimitSerializer(), write_only=True, required=False
    )
    options = OfferingOptionsSerializer(required=False)
    resource_options = OfferingOptionsSerializer(required=False)

    def validate(self, attrs):
        if not self.instance:
            if not has_permission(
                self.context["request"],
                PermissionEnum.CREATE_OFFERING,
                attrs["customer"],
            ):
                raise PermissionDenied()

        self._validate_customer(attrs)
        self._validate_attributes(attrs)
        self._validate_plans(attrs)

        attrs.setdefault("options", {"options": {}, "order": []})
        attrs.setdefault("resource_options", {"options": {}, "order": []})

        return attrs

    def validate_type(self, offering_type):
        if offering_type not in plugins.manager.backends.keys():
            raise rf_exceptions.ValidationError(_("Invalid value."))
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
        category = attrs.get("category")
        if category is None and self.instance:
            category = self.instance.category

        attributes = attrs.get("attributes")
        if attributes is not None and not isinstance(attributes, dict):
            raise rf_exceptions.ValidationError(
                {
                    "attributes": _("Dictionary is expected."),
                }
            )

        if attributes is None and self.instance:
            return

        if attributes is None:
            attributes = dict()

        validate_attributes(attributes, category)

    def _validate_customer(self, attrs):
        customer = attrs.get("customer")

        service_provider = models.ServiceProvider.objects.filter(
            customer=customer
        ).first()

        if service_provider is None:
            raise serializers.ValidationError(
                {"customer": _("Customer should be a service provider.")}
            )

    def validate_options(self, options):
        serializer = OfferingOptionsSerializer(data=options)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def _validate_plans(self, attrs):
        custom_components = attrs.get("components")
        if not custom_components and self.instance:
            custom_components = self.instance.components.all().values()

        offering_type = attrs.get("type", getattr(self.instance, "type", None))
        builtin_components = plugins.manager.get_components(offering_type)

        if builtin_components and attrs.get("components"):
            if {c.get("type") for c in attrs.get("components")} - {
                c.type for c in builtin_components
            }:
                raise serializers.ValidationError(
                    {"components": _("Extra components are not allowed.")}
                )

    def _create_plans(self, offering, plans):
        for plan_data in plans:
            create_plan(offering, plan_data)

    def _update_limits(self, offering, limits):
        for key, values in limits.items():
            min_value = values.get("min_value") or values.get("min")
            max_value = values.get("max_value") or values.get("max")
            max_available_limit = values.get("max_available_limit")

            models.OfferingComponent.objects.filter(offering=offering, type=key).update(
                min_value=min_value,
                max_value=max_value,
                max_available_limit=max_available_limit,
                article_code=values.get("article_code", ""),
            )

    def validate_plans(self, plans):
        if len(plans) < 1:
            raise serializers.ValidationError(
                {"plans": _("At least one plan should be specified.")}
            )
        return plans

    @transaction.atomic
    def create(self, validated_data):
        plans = validated_data.pop("plans", [])

        limits = validated_data.pop("limits", {})

        if not limits:
            custom_components = []
            limits = {}

            for component in validated_data.pop("components", []):
                if component["type"] in [
                    c.type
                    for c in plugins.manager.get_components(validated_data["type"])
                ]:
                    limits[component["type"]] = component
                else:
                    custom_components.append(component)
        else:
            custom_components = validated_data.pop("components", [])

        offering = super().create(validated_data)
        utils.create_offering_components(offering, custom_components)
        if limits:
            self._update_limits(offering, limits)
        self._create_plans(offering, plans)

        return offering


class OfferingPauseSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Offering
        fields = ["paused_reason"]


class PlanUpdateSerializer(BaseProviderPlanSerializer):
    class Meta(BaseProviderPlanSerializer.Meta):
        extra_kwargs = {
            "uuid": {"read_only": False},
        }


def update_plan_details(plan, data):
    plan_fields_that_cannot_be_edited = (
        plugins.manager.get_plan_fields_that_cannot_be_edited(plan.offering.type)
    )
    PLAN_FIELDS = {
        "name",
        "description",
        "unit",
        "max_amount",
        "article_code",
    }.difference(set(plan_fields_that_cannot_be_edited))

    for key in PLAN_FIELDS:
        if key in data:
            setattr(plan, key, data.get(key))
    plan.save()


class OfferingLocationUpdateSerializer(serializers.ModelSerializer):
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()

    class Meta:
        model = models.Offering
        fields = (
            "latitude",
            "longitude",
        )


class OfferingDescriptionUpdateSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.Offering
        fields = ("category",)

        related_paths = {
            "category": ("uuid", "title"),
        }

        extra_kwargs = {
            "category": {
                "lookup_field": "uuid",
                "view_name": "marketplace-category-detail",
            },
        }


class OfferingOverviewUpdateSerializer(
    core_serializers.SlugSerializerMixin,
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
            "name",
            "description",
            "full_description",
            "terms_of_service",
            "terms_of_service_link",
            "privacy_policy_link",
            "access_url",
            "getting_started",
            "integration_guide",
            "slug",
        )


class OfferingOptionsUpdateSerializer(serializers.ModelSerializer):
    options = OfferingOptionsSerializer()

    class Meta:
        model = models.Offering
        fields = ("options",)


class OfferingResourceOptionsUpdateSerializer(serializers.ModelSerializer):
    resource_options = OfferingOptionsSerializer()

    class Meta:
        model = models.Offering
        fields = ("resource_options",)


def update_or_create_service_settings_for_offering(
    offering: models.Offering, service_attributes: dict, certificate: str | None = None
):
    service_type = plugins.manager.get_service_type(offering.type)

    if not service_type:
        return

    if not offering.scope:
        offering.scope = structure_models.ServiceSettings.objects.create(
            name=offering.name,
            customer=offering.customer,
            type=service_type,
            shared=offering.shared,
        )
        offering.save()

    if certificate:
        offering.scope.options["certificate"] = certificate
    else:
        offering.scope.options.pop("certificate", None)

    offering.scope.save()

    if not service_attributes:
        return

    options_serializer_class = get_options_serializer_class(service_type)
    options_serializer = options_serializer_class(
        instance=offering.scope, data=service_attributes
    )
    for field in options_serializer.fields.values():
        field.required = False
        field.default = serializers.empty
    options_serializer.is_valid(raise_exception=True)
    update_fields = set()
    for key in (
        "backend_url",
        "username",
        "password",
        "domain",
        "token",
        "options",
    ):
        if key not in service_attributes and key != "options":
            continue
        value = options_serializer.validated_data.get(key)
        if value == serializers.empty:
            continue
        if key == "options":
            if isinstance(value, dict):
                offering.scope.options.update(value)
        else:
            setattr(offering.scope, key, value)
        update_fields.add(key)
    if update_fields:
        offering.scope.save(update_fields=update_fields)


class OfferingIntegrationUpdateSerializer(serializers.ModelSerializer):
    service_attributes = serializers.JSONField(required=False)
    secret_options = MergedSecretOptionsSerializer(required=False)
    plugin_options = MergedPluginOptionsSerializer(required=False)

    class Meta:
        model = models.Offering
        fields = (
            "secret_options",
            "plugin_options",
            "service_attributes",
            "backend_id",
        )

    def _update_service_attributes(self, instance, validated_data):
        service_attributes = validated_data.pop("service_attributes", {})
        certificate = validated_data.get("secret_options", {}).get(
            "openstack_api_tls_certificate"
        )

        update_or_create_service_settings_for_offering(
            instance, service_attributes, certificate
        )

        if instance.scope and instance.scope.state == CoreStates.CREATION_SCHEDULED:
            transaction.on_commit(
                lambda: ServiceSettingsCreateExecutor.execute(instance.scope)
            )

    def _update_secret_options(self, instance, validated_data):
        secret_options = validated_data.pop("secret_options", {})
        for key, value in secret_options.items():
            instance.secret_options[key] = value
        instance.save()

    def _update_plugin_options(self, instance, validated_data):
        plugin_options = validated_data.pop("plugin_options", {})
        for key, value in plugin_options.items():
            instance.plugin_options[key] = value
        instance.save()

    @transaction.atomic
    def update(self, instance, validated_data):
        self._update_service_attributes(instance, validated_data)
        self._update_secret_options(instance, validated_data)
        self._update_plugin_options(instance, validated_data)
        offering = super().update(instance, validated_data)
        return offering


class OfferingPermissionSerializer(
    structure_serializers.BasePermissionSerializer,
):
    offering = serializers.HyperlinkedRelatedField(
        source="scope",
        view_name="marketplace-provider-offering-detail",
        read_only=True,
        lookup_field="uuid",
    )
    offering_name = serializers.CharField(read_only=True, source="scope.name")
    offering_slug = serializers.CharField(read_only=True, source="scope.slug")
    offering_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")

    class Meta(structure_serializers.BasePermissionSerializer.Meta):
        model = UserRole
        fields = (
            "url",
            "pk",
            "created",
            "expiration_time",
            "created_by",
            "offering",
            "offering_uuid",
            "offering_slug",
            "offering_name",
        ) + structure_serializers.BasePermissionSerializer.Meta.fields
        protected_fields = ("offering", "user", "created_by", "created")
        view_name = "marketplace-offering-permission-detail"
        extra_kwargs = {
            "user": {
                "view_name": "user-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
            "created_by": {
                "view_name": "user-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
        }


class OfferingPermissionLogSerializer(OfferingPermissionSerializer):
    class Meta(OfferingPermissionSerializer.Meta):
        view_name = "marketplace-offering-permission-log-detail"


class ComponentQuotaSerializer(serializers.ModelSerializer):
    type = serializers.ReadOnlyField(source="component.type")

    class Meta:
        model = models.ComponentQuota
        fields = ("type", "limit", "usage")


class BaseItemSerializer(
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    plan = PublicPlanField(
        lookup_field="uuid",
        lookup_url_kwarg="plan_uuid",
        view_name="marketplace-public-offering-plan-detail",
        queryset=models.Plan.objects.all(),
        required=False,
    )

    class Meta:
        fields = (
            "offering",
            "offering_name",
            "offering_uuid",
            "offering_description",
            "offering_image",
            "offering_thumbnail",
            "offering_type",
            "offering_terms_of_service",
            "offering_shared",
            "offering_billable",
            "offering_plugin_options",
            "provider_name",
            "provider_uuid",
            "category_title",
            "category_uuid",
            "category_icon",
            "plan",
            "plan_unit",
            "plan_name",
            "plan_uuid",
            "plan_description",
            "attributes",
            "limits",
            "uuid",
            "created",
            "modified",
        )
        related_paths = {
            "offering": (
                "name",
                "uuid",
                "description",
                "image",
                "thumbnail",
                "type",
                "terms_of_service",
                "shared",
                "billable",
                "plugin_options",
            ),
            "plan": ("unit", "uuid", "name", "description"),
        }
        protected_fields = ("offering",)
        extra_kwargs = {
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-public-offering-detail",
            },
        }

    provider_name = serializers.ReadOnlyField(source="offering.customer.name")
    provider_uuid = serializers.UUIDField(
        read_only=True, source="offering.customer.uuid"
    )
    category_title = serializers.ReadOnlyField(source="offering.category.title")
    category_icon = serializers.ImageField(
        source="offering.category.icon", read_only=True
    )
    category_uuid = serializers.UUIDField(
        read_only=True, source="offering.category.uuid"
    )
    offering_thumbnail = serializers.ImageField(
        source="offering.thumbnail", read_only=True
    )
    offering_image = serializers.ImageField(source="offering.image", read_only=True)

    def validate_offering(self, offering):
        if not offering.state == OfferingStates.ACTIVE:
            raise rf_exceptions.ValidationError(_("Offering is not available."))
        return offering

    def validate(self, attrs):
        offering = attrs.get("offering")
        plan = attrs.get("plan")

        if not offering:
            if not self.instance:
                raise rf_exceptions.ValidationError(
                    {"offering": _("This field is required.")}
                )
            offering = self.instance.offering

        if plan:
            if plan.offering != offering:
                raise rf_exceptions.ValidationError(
                    {"plan": _("This plan is not available for selected offering.")}
                )

            validate_plan(plan)

        if offering.options:
            validate_options(
                offering.options.get("options", {}), attrs.get("attributes")
            )

        limits = attrs.get("limits")
        if limits:
            utils.validate_limits(limits, offering)
        return attrs

    def get_fields(self):
        fields = super().get_fields()
        method = self.context["view"].request.method

        if method == "GET" and "attributes" in fields:
            fields["attributes"] = serializers.ReadOnlyField(source="safe_attributes")
        return fields


class BaseRequestSerializer(BaseItemSerializer):
    type = NaturalChoiceField(
        choices=models.RequestTypeMixin.Types.CHOICES,
        required=False,
        default=models.RequestTypeMixin.Types.CREATE,
    )

    class Meta(BaseItemSerializer.Meta):
        fields = BaseItemSerializer.Meta.fields + ("type",)


class BaseOrderSerializer(BaseRequestSerializer):
    class Meta(BaseRequestSerializer.Meta):
        model = models.Order
        fields = BaseRequestSerializer.Meta.fields + (
            "resource_uuid",
            "resource_type",
            "resource_name",
            "cost",
            "state",
            "output",
            "marketplace_resource_uuid",
            "error_message",
            "error_traceback",
            "accepting_terms_of_service",
            "callback_url",
            "completed_at",
        )

        read_only_fields = (
            "cost",
            "state",
            "error_message",
            "error_traceback",
            "output",
            "completed_at",
        )
        protected_fields = ("offering", "plan", "callback_url")

    marketplace_resource_uuid = serializers.UUIDField(
        read_only=True, source="resource.uuid"
    )
    resource_name = serializers.CharField(read_only=True, source="resource.name")
    resource_uuid = serializers.UUIDField(
        read_only=True, source="resource.backend_uuid", allow_null=True
    )
    resource_type = serializers.CharField(
        read_only=True, source="resource.backend_type", allow_null=True
    )
    state = serializers.SerializerMethodField()
    limits = serializers.DictField(child=serializers.IntegerField(), required=False)
    accepting_terms_of_service = serializers.BooleanField(
        required=False, write_only=True
    )

    def get_state(self, obj) -> OrderStatesType:
        return obj.get_state_display()

    def get_fields(self):
        fields = super().get_fields()
        user = self.context["view"].request.user
        # conceal detailed error message from non-system users
        if (
            not user.is_authenticated or (not user.is_staff and not user.is_support)
        ) and "error_traceback" in fields:
            del fields["error_traceback"]
        return fields


class OrderDetailsSerializer(BaseOrderSerializer):
    class Meta(BaseOrderSerializer.Meta):
        fields = BaseOrderSerializer.Meta.fields + (
            "consumer_reviewed_by",
            "consumer_reviewed_by_full_name",
            "consumer_reviewed_by_username",
            "consumer_reviewed_at",
            "provider_reviewed_by",
            "provider_reviewed_by_full_name",
            "provider_reviewed_by_username",
            "provider_reviewed_at",
            "created_by_username",
            "created_by_full_name",
            "created_by_civil_number",
            "customer_name",
            "customer_uuid",
            "customer_slug",
            "project_name",
            "project_uuid",
            "project_description",
            "project_slug",
            "old_plan_name",
            "new_plan_name",
            "old_plan_uuid",
            "new_plan_uuid",
            "old_cost_estimate",
            "new_cost_estimate",
            "can_terminate",
            "fixed_price",
            "activation_price",
            "termination_comment",
            "backend_id",
        )

    consumer_reviewed_by = serializers.ReadOnlyField(
        source="consumer_reviewed_by.username",
        allow_null=True,
    )
    consumer_reviewed_by_full_name = serializers.ReadOnlyField(
        source="consumer_reviewed_by.full_name",
        allow_null=True,
    )
    consumer_reviewed_by_username = serializers.ReadOnlyField(
        source="consumer_reviewed_by.username",
        allow_null=True,
    )
    consumer_reviewed_at = serializers.ReadOnlyField(
        allow_null=True,
    )
    provider_reviewed_by = serializers.ReadOnlyField(
        source="provider_reviewed_by.username",
        allow_null=True,
    )
    provider_reviewed_by_full_name = serializers.ReadOnlyField(
        source="provider_reviewed_by.full_name",
        allow_null=True,
    )
    provider_reviewed_by_username = serializers.ReadOnlyField(
        source="provider_reviewed_by.username",
        allow_null=True,
    )
    provider_reviewed_at = serializers.ReadOnlyField(
        allow_null=True,
    )

    created_by_username = serializers.ReadOnlyField(source="created_by.username")
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")
    created_by_civil_number = serializers.ReadOnlyField(
        source="created_by.civil_number",
        allow_null=True,
    )

    customer_name = serializers.ReadOnlyField(source="project.customer.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    customer_slug = serializers.ReadOnlyField(source="project.customer.slug")

    project_name = serializers.ReadOnlyField(source="project.name")
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_description = serializers.ReadOnlyField(source="project.description")
    project_slug = serializers.ReadOnlyField(source="project.slug")

    old_plan_name = serializers.ReadOnlyField(
        source="old_plan.name",
        allow_null=True,
    )
    new_plan_name = serializers.ReadOnlyField(
        source="plan.name",
        allow_null=True,
    )

    old_plan_uuid = serializers.UUIDField(
        read_only=True,
        source="old_plan.uuid",
        allow_null=True,
    )
    new_plan_uuid = serializers.UUIDField(
        read_only=True,
        source="plan.uuid",
        allow_null=True,
    )

    old_cost_estimate = serializers.ReadOnlyField(
        source="resource.cost",
        allow_null=True,
    )
    new_cost_estimate = serializers.ReadOnlyField(
        source="cost",
        allow_null=True,
    )

    can_terminate = serializers.SerializerMethodField()
    termination_comment = serializers.ReadOnlyField()

    def get_can_terminate(self, order: models.Order) -> bool:
        if not plugins.manager.can_cancel_order(order.offering.type):
            return False

        if order.state not in (
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.EXECUTING,
        ):
            return False

        return True


class OrderSetStateErredSerializer(
    serializers.ModelSerializer, core_serializers.AugmentedSerializerMixin
):
    class Meta:
        model = models.Order
        fields = ("error_message", "error_traceback")
        protected_fields = ("error_message", "error_traceback")


def validate_public_offering(order: models.Order):
    # Order is ok if organization groups are not defined for offering
    if not order.offering.organization_groups.count():
        return

    # Order is ok if consumer and provider organization groups match
    if (
        order.project.customer.organization_groups.exists()
        and order.offering.organization_groups.filter(
            id__in=order.project.customer.organization_groups.all()
        ).exists()
    ):
        return
    raise serializers.ValidationError(_("This offering is not available for ordering."))


def validate_private_offering(order: models.Order):
    # Order is ok if consumer and provider organization is the same
    if order.offering.customer == order.project.customer:
        return

    # Order is ok if consumer and provider project is the same
    if order.offering.project == order.project:
        return

    raise serializers.ValidationError(
        _('Offering "%s" is not allowed in organization "%s".')
        % (order.offering.name, order.project.customer.name)
    )


def check_pending_order_exists(resource):
    return models.Order.objects.filter(
        resource=resource,
        state__in=(
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.EXECUTING,
        ),
    ).exists()


def validate_order(order: models.Order, request):
    structure_utils.check_customer_blocked_or_archived(order.project.customer)

    if order.type != models.Order.Types.TERMINATE:
        structure_utils.check_project_end_date(order.project)

        if order.offering.state not in (
            OfferingStates.ACTIVE,
            OfferingStates.PAUSED,
        ):
            raise serializers.ValidationError(_("Offering is not available."))

    if order.offering.shared:
        validate_public_offering(order)
    else:
        validate_private_offering(order)

    if check_pending_order_exists(order.resource):
        raise serializers.ValidationError(
            _("Pending order for resource already exists.")
        )

    utils.validate_order(order, request)


class OrderCreateSerializer(
    BaseOrderSerializer,
    structure_serializers.PermissionFieldFilteringMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    project_name = serializers.ReadOnlyField(source="project.name")
    project_description = serializers.ReadOnlyField(source="project.description")
    customer_name = serializers.ReadOnlyField(source="project.customer.name")

    class Meta:
        model = models.Order
        fields = BaseOrderSerializer.Meta.fields + (
            "url",
            "uuid",
            "created",
            "created_by",
            "created_by_username",
            "created_by_full_name",
            "consumer_reviewed_by",
            "consumer_reviewed_at",
            "consumer_reviewed_by_username",
            "consumer_reviewed_by_full_name",
            "project",
            "project_uuid",
            "project_name",
            "project_description",
            "customer_name",
            "customer_uuid",
            "state",
            "cost",
            "type",
        )
        read_only_fields = (
            "created_by",
            "consumer_reviewed_by",
            "consumer_reviewed_at",
            "state",
            "cost",
        )
        protected_fields = ("project",)
        related_paths = {
            **BaseOrderSerializer.Meta.related_paths,
            "created_by": ("username", "full_name"),
            "consumer_reviewed_by": ("username", "full_name"),
            "project": ("uuid",),
        }
        extra_kwargs = {
            **BaseOrderSerializer.Meta.extra_kwargs,
            "url": {"lookup_field": "uuid"},
            "created_by": {"lookup_field": "uuid", "view_name": "user-detail"},
            "consumer_reviewed_by": {
                "lookup_field": "uuid",
                "view_name": "user-detail",
                "allow_null": True,
            },
            "consumer_reviewed_by_username": {
                "allow_null": True,
            },
            "consumer_reviewed_by_full_name": {
                "allow_null": True,
            },
            "project": {"lookup_field": "uuid", "view_name": "project-detail"},
        }

    error_message = serializers.ReadOnlyField()

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        project: structure_models.Project = validated_data["project"]
        resource = models.Resource(
            project=project,
            offering=validated_data["offering"],
            plan=validated_data.get("plan"),
            limits=validated_data.get("limits") or {},
            attributes=validated_data.get("attributes") or {},
            name=validated_data.get("attributes").get("name") or "",
        )
        resource.init_cost()
        attributes = validated_data.get("attributes", {})
        end_date = attributes.get("end_date")
        validate_end_date(resource, request.user, end_date)

        if end_date:
            resource.end_date = end_date
            resource.end_date_requested_by = request.user

        resource.save()

        order = models.Order(
            resource=resource,
            project=project,
            created_by=request.user,
            offering=validated_data["offering"],
            plan=validated_data.get("plan"),
            attributes=attributes,
            limits=validated_data.get("limits", {}),
            type=validated_data.get("type"),
        )
        validate_order(order, request)
        self.quotas_validate(order)
        order.init_cost()
        order.save()
        return order

    def get_filtered_field_names(self):
        return ("project",)

    def quotas_validate(self, order):
        try:
            if not order.offering.scope:
                return
        except AttributeError:
            return
        processor_class = manager.get_processor(
            order.offering.type, "create_resource_processor"
        )
        if not issubclass(processor_class, CreateResourceProcessor):
            return
        processor = processor_class(order)
        serializer_class = processor.get_serializer_class()
        if not serializer_class:
            return
        post_data = processor.get_post_data()
        serializer = serializer_class(data=post_data, context=self.context)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save()
                raise TransactionRollback()
        except TransactionRollback:
            pass

    def validate(self, attrs):
        attrs = super().validate(attrs)
        offering = attrs["offering"]

        if (
            offering.shared
            and offering.terms_of_service
            and not attrs.get("accepting_terms_of_service")
        ):
            raise ValidationError(
                _("Terms of service for offering '%s' have not been accepted.")
                % offering
            )

        return attrs


class BackendMetadataSerializer(serializers.Serializer):
    state = serializers.CharField(read_only=True)
    runtime_state = serializers.CharField(read_only=True)
    action = serializers.CharField(read_only=True)
    instance_name = serializers.CharField(read_only=True, allow_null=True)


class ResourceSuggestNameSerializer(serializers.ModelSerializer):
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.objects.all(), slug_field="uuid"
    )
    offering = serializers.SlugRelatedField(
        queryset=models.Offering.objects.all(), slug_field="uuid"
    )

    class Meta:
        model = models.Resource
        fields = ("project", "offering")

    def get_fields(self):
        fields = super().get_fields()

        request = self.context["request"]
        user = request.user
        fields["project"].queryset = filter_queryset_for_user(
            fields["project"].queryset, user
        )
        fields["offering"].queryset = fields[
            "offering"
        ].queryset.filter_by_ordering_availability_for_user(user)
        return fields


class ResourceSerializer(core_serializers.SlugSerializerMixin, BaseItemSerializer):
    class Meta(BaseItemSerializer.Meta):
        model = models.Resource
        fields = BaseItemSerializer.Meta.fields + (
            "url",
            "scope",
            "description",
            "state",
            "resource_uuid",
            "backend_id",
            "effective_id",
            "resource_type",
            "project",
            "project_uuid",
            "project_name",
            "project_description",
            "project_end_date",
            "project_end_date_requested_by",
            "customer_uuid",
            "customer_name",
            "offering_uuid",
            "offering_name",
            "parent_offering_uuid",
            "parent_offering_name",
            "parent_uuid",
            "parent_name",
            "backend_metadata",
            "is_usage_based",
            "is_limit_based",
            "name",
            "slug",
            "current_usages",
            "can_terminate",
            "report",
            "end_date",
            "end_date_requested_by",
            "username",
            "limit_usage",
            "downscaled",
            "restrict_member_access",
            "paused",
            "endpoints",
            "error_message",
            "error_traceback",
            "offering_customer_uuid",
            "options",
            "available_actions",
            "last_sync",
            "order_in_progress",
            "creation_order",
        )
        read_only_fields = (
            "backend_metadata",
            "scope",
            "current_usages",
            "backend_id",
            "effective_id",
            "report",
            "description",
            "limit_usage",
            "end_date_requested_by",
            "error_message",
            "error_traceback",
            "options",
            "restrict_member_access",
            "last_sync",
        )
        view_name = "marketplace-resource-detail"
        extra_kwargs = dict(
            **BaseItemSerializer.Meta.extra_kwargs,
            url={"lookup_field": "uuid"},
            end_date_requested_by={"lookup_field": "uuid", "view_name": "user-detail"},
        )

    state = serializers.SerializerMethodField()
    scope = core_serializers.GenericRelatedField(read_only=True)
    resource_uuid = serializers.UUIDField(
        read_only=True, source="backend_uuid", allow_null=True
    )
    resource_type = serializers.ReadOnlyField(source="backend_type")
    project = serializers.HyperlinkedRelatedField(
        lookup_field="uuid",
        view_name="project-detail",
        read_only=True,
    )
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.ReadOnlyField(source="project.name")
    project_end_date = serializers.ReadOnlyField(source="project.end_date")
    project_end_date_requested_by = serializers.HyperlinkedRelatedField(
        source="project.end_date_requested_by",
        lookup_field="uuid",
        view_name="user-detail",
        read_only=True,
    )
    project_description = serializers.ReadOnlyField(source="project.description")
    customer_name = serializers.ReadOnlyField(source="project.customer.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    parent_offering_uuid = serializers.UUIDField(
        read_only=True, source="offering.parent.uuid"
    )
    parent_offering_name = serializers.ReadOnlyField(source="offering.parent.name")
    parent_uuid = serializers.UUIDField(read_only=True, source="parent.uuid")
    parent_name = serializers.ReadOnlyField(source="parent.name")
    # If resource is usage-based, frontend would render button to show and report usage
    is_usage_based = serializers.ReadOnlyField(source="offering.is_usage_based")
    is_limit_based = serializers.ReadOnlyField(source="offering.is_limit_based")
    can_terminate = serializers.SerializerMethodField()
    report = ResourceReportField(read_only=True)
    username = serializers.SerializerMethodField()
    limit_usage = serializers.SerializerMethodField()
    endpoints = NestedEndpointSerializer(many=True, read_only=True)
    offering_customer_uuid = serializers.UUIDField(
        read_only=True, source="offering.customer.uuid"
    )
    available_actions = serializers.SerializerMethodField()
    limits = serializers.SerializerMethodField()
    attributes = serializers.SerializerMethodField()
    current_usages = serializers.SerializerMethodField()
    order_in_progress = serializers.SerializerMethodField(allow_null=True)
    creation_order = serializers.SerializerMethodField(allow_null=True)
    backend_metadata = serializers.SerializerMethodField()

    def get_can_terminate(self, resource) -> bool:
        view = self.context["view"]
        try:
            permissions.user_can_terminate_resource(view.request, view, resource)
        except APIException:
            return False
        except ObjectDoesNotExist:
            return False
        validator = core_validators.StateValidator(
            ResourceStates.OK, ResourceStates.ERRED
        )
        try:
            validator(resource)
        except APIException:
            return False

        try:
            structure_utils.check_customer_blocked_or_archived(resource.project)
        except ValidationError:
            return False

        return not check_pending_order_exists(resource)

    def get_username(self, resource) -> str | None:
        user = self.context["request"].user
        offering_user = models.OfferingUser.objects.filter(
            offering=resource.offering, user=user
        ).first()
        if offering_user:
            return offering_user.username

    def get_limit_usage(self, resource: models.Resource) -> float | None:
        if not resource.offering.is_limit_based or not resource.plan:
            return

        limit_usage = {}

        limit_components: list[models.OfferingComponent] = (
            resource.offering.components.filter(
                billing_type=models.OfferingComponent.BillingTypes.LIMIT
            )
        )

        for component in limit_components:
            if component.limit_period in (
                None,
                models.OfferingComponent.LimitPeriods.MONTH,
            ):
                limit_usage[component.type] = resource.current_usages.get(
                    component.type
                )
                continue
            usages = models.ComponentUsage.objects.filter(
                resource=resource, component=component
            ).exclude(plan_period=None)
            if component.limit_period == models.OfferingComponent.LimitPeriods.ANNUAL:
                usages = usages.filter(date__year__gte=datetime.date.today().year)
            limit_usage[component.type] = usages.aggregate(total=Sum("usage"))["total"]

        return limit_usage

    def get_available_actions(self, resource: models.Resource) -> list[str]:
        return plugins.manager.get_available_resource_actions(resource)

    def get_limits(self, resource: models.Resource) -> dict[str, int]:
        return resource.limits

    def get_attributes(self, resource: models.Resource) -> dict:
        return resource.safe_attributes

    def get_current_usages(self, resource: models.Resource) -> dict[str, int]:
        return resource.current_usages

    @extend_schema_field(BackendMetadataSerializer)
    def get_backend_metadata(self, resource: models.Resource):
        return resource.backend_metadata

    @extend_schema_field(OrderDetailsSerializer)
    def get_order_in_progress(self, resource: models.Resource):
        if resource.order_in_progress:
            return OrderDetailsSerializer(
                instance=resource.order_in_progress, context=self.context
            ).data

    @extend_schema_field(OrderDetailsSerializer)
    def get_creation_order(self, resource: models.Resource):
        if resource.creation_order:
            return OrderDetailsSerializer(
                instance=resource.creation_order, context=self.context
            ).data

    def get_state(self, resource: models.Resource) -> ResourceStatesType:
        return resource.get_state_display()

    def get_fields(self):
        fields = super().get_fields()
        if "attributes" in fields:
            fields["attributes"] = serializers.SerializerMethodField()
        query_params = self.context["request"].query_params
        keys = query_params.getlist(self.FIELDS_PARAM_NAME)
        for key in ("order_in_progress", "creation_order"):
            if keys and key not in keys and key in fields:
                del fields[key]
        return fields


class OrderUUIDSerializer(serializers.Serializer):
    order_uuid = serializers.UUIDField(read_only=True)


class ResourceSwitchPlanSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("plan",)

    plan = PublicPlanField(
        lookup_field="uuid",
        lookup_url_kwarg="plan_uuid",
        view_name="marketplace-public-offering-plan-detail",
        queryset=models.Plan.objects.all(),
        required=True,
    )

    def validate(self, attrs):
        plan = attrs["plan"]
        resource = self.context["view"].get_object()

        if plan.offering != resource.offering:
            raise rf_exceptions.ValidationError(
                {"plan": _("Plan is not available for this offering.")}
            )

        validate_plan(plan)
        return attrs


class ResourceUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = (
            "name",
            "description",
            "end_date",
        )

    def validate_end_date(self, end_date):
        if not end_date:
            return
        if end_date < timezone.datetime.today().date():
            raise serializers.ValidationError(
                {"end_date": _("Cannot be earlier than the current date.")}
            )
        return end_date

    def save(self, **kwargs):
        resource = super().save(**kwargs)
        user = self.context["request"].user

        validate_end_date(resource, user, self.validated_data.get("end_date"))
        resource.save()
        log.log_marketplace_resource_end_date_has_been_updated(resource, user)


class ResourceEndDateByProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("end_date",)

    def validate_end_date(self, end_date):
        if not end_date:
            return
        invoice_threshold = timezone.datetime.today() - datetime.timedelta(days=90)
        if InvoiceItem.objects.filter(
            invoice__created__gt=invoice_threshold, resource=self.instance
        ).exists():
            raise serializers.ValidationError(
                _(
                    "Service provider can not set end date of the resource which has been used for the last 90 days."
                )
            )

        min_end_date = timezone.datetime.today() + datetime.timedelta(days=7)
        if end_date < min_end_date.date():
            raise serializers.ValidationError(
                _("Please set at least 7 days in advance.")
            )
        return end_date

    def save(self, **kwargs):
        resource = super().save(**kwargs)
        user = self.context["request"].user
        resource.end_date_requested_by = user
        resource.save(update_fields=["end_date_requested_by"])


class ResourceBackendMetadataSerializer(serializers.ModelSerializer):
    backend_metadata = serializers.JSONField(required=True)

    class Meta:
        model = models.Resource
        fields = ("backend_metadata",)


class ResourceUpdateLimitsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("limits",)

    limits = serializers.DictField(
        child=serializers.IntegerField(min_value=0), required=True
    )


class ResourceBackendIDSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("backend_id",)


class ResourceSlugSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("slug",)


class ResourceStateSerializer(serializers.Serializer):
    state = serializers.ChoiceField(["ok", "erred", "terminated"])


class ResourceOptionsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("options",)

    def validate_options(self, attrs):
        resource: models.Resource = self.instance
        resource_options = resource.offering.resource_options
        if not resource_options or not resource_options.get("options"):
            raise serializers.ValidationError(
                "Metadata for resource options is not defined."
            )
        validate_options(resource_options["options"], attrs, optional=True)
        if self.instance.options:
            return {**self.instance.options, **attrs}
        else:
            return attrs


class ResourceOfferingSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Offering
        fields = ("name", "uuid")


class BaseComponentSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.Serializer
):
    type = serializers.ReadOnlyField(source="component.type")
    name = serializers.ReadOnlyField(source="component.name")
    measured_unit = serializers.ReadOnlyField(source="component.measured_unit")


class CategoryComponentUsageSerializer(
    BaseComponentSerializer,
    serializers.ModelSerializer,
):
    category_title = serializers.ReadOnlyField(source="component.category.title")
    category_uuid = serializers.UUIDField(
        read_only=True, source="component.category.uuid"
    )
    scope = core_serializers.GenericRelatedField(
        related_models=(structure_models.Project, structure_models.Customer)
    )

    class Meta:
        model = models.CategoryComponentUsage
        fields = (
            "name",
            "type",
            "measured_unit",
            "category_title",
            "category_uuid",
            "date",
            "reported_usage",
            "fixed_usage",
            "scope",
        )


class BaseComponentUsageSerializer(
    BaseComponentSerializer, serializers.ModelSerializer
):
    class Meta:
        model = models.ComponentUsage
        fields = (
            "uuid",
            "created",
            "description",
            "type",
            "name",
            "measured_unit",
            "usage",
            "date",
            "recurring",
        )


class ComponentUsageSerializer(BaseComponentUsageSerializer):
    resource_name = serializers.ReadOnlyField(source="resource.name")
    resource_uuid = serializers.UUIDField(read_only=True, source="resource.uuid")

    offering_name = serializers.ReadOnlyField(source="resource.offering.name")
    offering_uuid = serializers.UUIDField(
        read_only=True, source="resource.offering.uuid"
    )

    project_uuid = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()

    customer_name = serializers.SerializerMethodField()
    customer_uuid = serializers.SerializerMethodField()

    # TODO: temporary functionality, remove after full migration to the new SLURM plugin
    usage = serializers.SerializerMethodField()

    class Meta(BaseComponentUsageSerializer.Meta):
        fields = BaseComponentUsageSerializer.Meta.fields + (
            "resource_name",
            "resource_uuid",
            "offering_name",
            "offering_uuid",
            "project_name",
            "project_uuid",
            "customer_name",
            "customer_uuid",
            "recurring",
            "billing_period",
            "modified_by",
        )

    def get_project_uuid(self, instance) -> str:
        return instance.resource.project.uuid

    def get_project_name(self, instance) -> str:
        return instance.resource.project.name

    def get_customer_uuid(self, instance) -> str:
        return instance.resource.project.customer.uuid

    def get_customer_name(self, instance) -> str:
        return instance.resource.project.customer.name

    def get_usage(self, instance) -> int:
        # TODO: temporary functionality, remove after full migration to the new SLURM plugin
        from waldur_mastermind.marketplace_slurm import PLUGIN_NAME as SLURM_PLUGIN_NAME
        from waldur_mastermind.marketplace_slurm import (
            registrators as slurm_registrators,
        )

        if (
            instance.plan_period is None
            or instance.plan_period.plan.offering.type != SLURM_PLUGIN_NAME
        ):
            return instance.usage

        converted_usage = slurm_registrators.SlurmRegistrator.convert_quantity(
            instance.usage, instance.component.type
        )
        return converted_usage

    def get_fields(self):
        fields = super().get_fields()

        query_fields = self.context["request"].query_params.getlist("field")

        if self.instance and query_fields:
            selected_field = {}

            for f in fields.keys():
                if f in query_fields:
                    selected_field[f] = fields[f]

            return selected_field

        return fields


class ComponentUserUsageSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    user = serializers.HyperlinkedRelatedField(
        queryset=models.OfferingUser.objects.all(),
        view_name="marketplace-offering-user-detail",
        lookup_field="uuid",
    )
    component_usage = serializers.HyperlinkedRelatedField(
        queryset=models.ComponentUsage.objects.all(),
        view_name="marketplace-component-usage-detail",
        lookup_field="uuid",
    )
    measured_unit = serializers.ReadOnlyField(
        source="component_usage.component.measured_unit"
    )
    component_type = serializers.ReadOnlyField(source="component_usage.component.type")
    date = serializers.ReadOnlyField(source="component_usage.date")
    billing_period = serializers.ReadOnlyField(source="component_usage.billing_period")

    resource_name = serializers.ReadOnlyField(source="component_usage.resource.name")
    resource_uuid = serializers.UUIDField(
        read_only=True, source="component_usage.resource.uuid"
    )

    offering_name = serializers.ReadOnlyField(
        source="component_usage.resource.offering.name"
    )
    offering_uuid = serializers.UUIDField(
        read_only=True, source="component_usage.resource.offering.uuid"
    )

    project_uuid = serializers.UUIDField(
        read_only=True, source="component_usage.resource.project.uuid"
    )
    project_name = serializers.ReadOnlyField(
        source="component_usage.resource.project.name"
    )

    customer_name = serializers.ReadOnlyField(
        source="component_usage.resource.project.customer.name"
    )
    customer_uuid = serializers.UUIDField(
        read_only=True, source="component_usage.resource.project.customer.uuid"
    )
    # TODO: temporary functionality, remove after full migration to the new SLURM plugin
    usage = serializers.SerializerMethodField()

    class Meta:
        fields = (
            "uuid",
            "user",
            "username",
            "component_usage",
            "usage",
            "measured_unit",
            "description",
            "created",
            "modified",
            "backend_id",
            "resource_name",
            "resource_uuid",
            "offering_name",
            "offering_uuid",
            "project_name",
            "project_uuid",
            "customer_name",
            "customer_uuid",
            "component_type",
            "date",
            "billing_period",
        )
        model = models.ComponentUserUsage

    def get_usage(self, instance) -> int:
        # TODO: temporary functionality, remove after full migration to the new SLURM plugin
        from waldur_mastermind.marketplace_slurm import PLUGIN_NAME as SLURM_PLUGIN_NAME
        from waldur_mastermind.marketplace_slurm import (
            registrators as slurm_registrators,
        )

        # The first check ensures that the second one doesn't fail is the plan period is None
        if (
            instance.component_usage.plan_period is None
            or instance.component_usage.plan_period.plan.offering.type
            != SLURM_PLUGIN_NAME
        ):
            return instance.usage

        converted_usage = slurm_registrators.SlurmRegistrator.convert_quantity(
            instance.usage, instance.component_usage.component.type
        )
        return converted_usage


class ComponentUserUsageCreateSerializer(serializers.ModelSerializer):
    user = serializers.HyperlinkedRelatedField(
        queryset=models.OfferingUser.objects.all(),
        view_name="marketplace-offering-user-detail",
        lookup_field="uuid",
        required=False,
    )

    def validate(self, attrs):
        user = attrs.get("user")
        component_usage = self.context["view"].get_object()
        new_usage = attrs.get("usage", 0)

        usage_limit = models.ComponentUserUsageLimit.objects.filter(
            resource=component_usage.resource,
            component=component_usage.component,
            user=user,
        ).first()

        if usage_limit:
            total_usage = (
                models.ComponentUserUsage.objects.filter(
                    user=user, component_usage=component_usage
                ).aggregate(total=Sum("usage"))["total"]
                or 0
            )

            if total_usage + new_usage > usage_limit.limit:
                raise serializers.ValidationError(
                    f"Usage limit exceeded. Maximum allowed: {usage_limit.limit}, "
                    f"current usage: {total_usage}, additional: {new_usage}."
                )

        return attrs

    class Meta:
        model = models.ComponentUserUsage
        fields = (
            "usage",
            "username",
            "user",
        )


class ResourcePlanPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ResourcePlanPeriod
        fields = ("uuid", "plan_name", "plan_uuid", "start", "end", "components")

    plan_name = serializers.ReadOnlyField(source="plan.name")
    plan_uuid = serializers.UUIDField(read_only=True, source="plan.uuid")
    components = BaseComponentUsageSerializer(source="current_components", many=True)


class ImportResourceSerializer(serializers.Serializer):
    backend_id = serializers.CharField()
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.available_objects.all(), slug_field="uuid"
    )
    plan = serializers.SlugRelatedField(
        queryset=models.Plan.objects.all(), slug_field="uuid", required=False
    )

    def get_fields(self):
        fields = super().get_fields()

        request = self.context["request"]
        user = request.user
        fields["project"].queryset = filter_queryset_for_user(
            fields["project"].queryset, user
        )
        return fields


class ServiceProviderSignatureSerializer(serializers.Serializer):
    customer = serializers.SlugRelatedField(
        queryset=structure_models.Customer.objects.all(), slug_field="uuid"
    )
    data = serializers.CharField()
    dry_run = serializers.BooleanField(default=False, required=False)

    def validate(self, attrs):
        customer = attrs["customer"]
        service_provider = getattr(customer, "serviceprovider", None)
        api_secret_code = service_provider and service_provider.api_secret_code

        if not api_secret_code:
            raise rf_exceptions.ValidationError(_("API secret code is not set."))

        try:
            data = core_utils.decode_jwt_token(attrs["data"], api_secret_code)
            attrs["data"] = data
            return attrs
        except jwt.exceptions.DecodeError:
            raise rf_exceptions.ValidationError(_("Signature verification failed."))


class ComponentUsageItemSerializer(serializers.Serializer):
    type = serializers.CharField()
    amount = serializers.DecimalField(decimal_places=2, max_digits=20)
    description = serializers.CharField(required=False, allow_blank=True)
    recurring = serializers.BooleanField(default=False)


class ComponentUsageCreateSerializer(serializers.Serializer):
    usages = ComponentUsageItemSerializer(many=True)
    plan_period = serializers.SlugRelatedField(
        queryset=models.ResourcePlanPeriod.objects.all(),
        slug_field="uuid",
        required=False,
    )
    resource = serializers.SlugRelatedField(
        queryset=models.Resource.objects.all(), slug_field="uuid", required=False
    )

    def validate_plan_period(self, plan_period):
        date = datetime.date.today()
        if plan_period.end and plan_period.end < core_utils.month_start(date):
            raise serializers.ValidationError(_("Billing period is closed."))
        return plan_period

    @classmethod
    def get_components_map(cls, offering) -> dict[str, models.OfferingComponent]:
        # Allow to report usage for limit-based components
        components = offering.components.filter(
            billing_type__in=[BillingTypes.USAGE, BillingTypes.LIMIT]
        )
        return {component.type: component for component in components}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        plan_period = attrs.get("plan_period")
        resource = plan_period and plan_period.resource or attrs.get("resource")
        if not resource:
            raise rf_exceptions.ValidationError(
                _("Either plan_period or resource should be provided.")
            )
        offering = resource.offering

        States = ResourceStates
        if resource.state not in (States.OK, States.UPDATING, States.TERMINATING):
            raise rf_exceptions.ValidationError(
                {"resource": _("Resource is not in valid state.")}
            )

        valid_components = set(self.get_components_map(offering))
        actual_components = {usage["type"] for usage in attrs["usages"]}

        invalid_components = ", ".join(sorted(actual_components - valid_components))

        if invalid_components:
            raise rf_exceptions.ValidationError(
                _("These components are invalid: %s.") % invalid_components
            )

        return attrs

    def save(self):
        plan_period = self.validated_data.get("plan_period")
        resource = (
            plan_period and plan_period.resource or self.validated_data.get("resource")
        )

        components_map = self.get_components_map(resource.plan.offering)
        now = timezone.now()
        billing_period = core_utils.month_start(now)
        user: User = self.context["request"].user
        if user.is_anonymous:
            user = None

        for usage in self.validated_data["usages"]:
            amount = usage["amount"]
            description = usage.get("description", "")
            component = components_map[usage["type"]]
            recurring = usage["recurring"]
            if component.billing_type == models.OfferingComponent.BillingTypes.USAGE:
                component.validate_amount(resource, amount, now)

            models.ComponentUsage.objects.filter(
                resource=resource,
                component=component,
                billing_period=billing_period,
            ).update(recurring=False)

            if not plan_period:
                plan_period = utils.get_plan_period(resource, now)

            usage, created = models.ComponentUsage.objects.update_or_create(
                resource=resource,
                component=component,
                plan_period=plan_period,
                billing_period=billing_period,
                defaults={
                    "usage": amount,
                    "date": now,
                    "description": description,
                    "recurring": recurring,
                    "modified_by": user,
                },
            )
            if created:
                logger.info(
                    f"Usage has been created for {resource}, component: {component.type}, value: {amount}"
                )
            else:
                logger.info(
                    f"Usage has been updated for {resource}, component: {component.type}, value: {amount}"
                )
        resource.current_usages = {
            usage["type"]: str(usage["amount"])
            for usage in self.validated_data["usages"]
        }
        resource.save(update_fields=["current_usages"])


class OfferingFileSerializer(
    core_serializers.RestrictedSerializerMixin,
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.OfferingFile
        fields = (
            "url",
            "uuid",
            "name",
            "offering",
            "created",
            "file",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid"},
            offering={
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        )


class OfferingReferralSerializer(
    serializers.HyperlinkedModelSerializer,
    core_serializers.AugmentedSerializerMixin,
):
    scope = core_serializers.GenericRelatedField(read_only=True)
    scope_uuid = serializers.UUIDField(read_only=True, source="scope.uuid")

    class Meta:
        model = pid_models.DataciteReferral
        fields = (
            "url",
            "uuid",
            "scope",
            "scope_uuid",
            "pid",
            "relation_type",
            "resource_type",
            "creator",
            "publisher",
            "published",
            "title",
            "referral_url",
        )
        extra_kwargs = dict(
            url={
                "lookup_field": "uuid",
                "view_name": "marketplace-offering-referral-detail",
            },
            offering={
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        )


class OfferingUserSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
    user_username = serializers.ReadOnlyField(source="user.username")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="offering.customer.uuid"
    )
    customer_name = serializers.ReadOnlyField(source="offering.customer.name")
    is_restricted = serializers.ReadOnlyField()

    class Meta:
        model = models.OfferingUser
        fields = (
            "url",
            "uuid",
            "user",
            "offering",
            "username",
            "offering_uuid",
            "offering_name",
            "user_uuid",
            "user_username",
            "user_full_name",
            "created",
            "modified",
            "customer_uuid",
            "customer_name",
            "is_restricted",
        )
        extra_kwargs = dict(
            offering={
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
            user={"lookup_field": "uuid", "view_name": "user-detail"},
            url={
                "lookup_field": "uuid",
                "view_name": "marketplace-offering-user-detail",
            },
        )

    def create(self, validated_data):
        request = self.context["request"]
        offering = validated_data["offering"]

        if not has_permission(
            request, PermissionEnum.CREATE_OFFERING_USER, offering.customer
        ):
            raise rf_exceptions.PermissionDenied()

        if not offering.plugin_options.get("service_provider_can_create_offering_user"):
            raise rf_exceptions.ValidationError(
                _("It is not allowed to create users for current offering.")
            )

        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context["request"]
        offering = instance.offering

        if not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, offering.customer
        ):
            raise rf_exceptions.PermissionDenied()

        return super().update(instance, validated_data)


class OfferingUserUpdateRestrictionSerializer(serializers.Serializer):
    is_restricted = serializers.BooleanField()

    def validate(self, attrs):
        request = self.context["request"]
        offering_user = self.instance
        offering = offering_user.offering
        if not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER_RESTRICTION, offering.customer
        ):
            raise rf_exceptions.PermissionDenied()
        return attrs


class FilterForUserField(serializers.HyperlinkedRelatedField):
    def get_queryset(self):
        user = self.context["request"].user
        return self.queryset.filter_for_user(user)


class OfferingUserRoleSerializer(serializers.HyperlinkedModelSerializer):
    offering = FilterForUserField(
        lookup_field="uuid",
        view_name="marketplace-provider-offering-detail",
        queryset=models.Offering.objects.all(),
    )
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")

    class Meta:
        model = models.OfferingUserRole
        fields = (
            "name",
            "uuid",
            "offering",
            "offering_uuid",
            "offering_name",
        )


class ResourceUserSerializer(serializers.HyperlinkedModelSerializer):
    resource = serializers.HyperlinkedRelatedField(
        lookup_field="uuid",
        view_name="marketplace-resource-detail",
        queryset=models.Resource.objects.all(),
    )
    resource_uuid = serializers.UUIDField(read_only=True, source="resource.uuid")
    role_uuid = serializers.UUIDField(read_only=True, source="role.uuid")
    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
    resource_name = serializers.ReadOnlyField(source="resource.name")
    role_name = serializers.ReadOnlyField(source="role.name")
    user_username = serializers.ReadOnlyField(source="user.username")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")

    class Meta:
        model = models.ResourceUser
        fields = (
            "uuid",
            "resource",
            "role",
            "user",
            "resource_uuid",
            "role_uuid",
            "user_uuid",
            "resource_name",
            "role_name",
            "user_username",
            "user_full_name",
        )
        extra_kwargs = dict(
            user={"lookup_field": "uuid", "view_name": "user-detail"},
            role={
                "lookup_field": "uuid",
                "view_name": "marketplace-offering-user-role-detail",
            },
        )

    def get_fields(self):
        fields = super().get_fields()
        user = self.context["request"].user
        queryset: ResourceQuerySet = fields["resource"].queryset
        fields["resource"].queryset = queryset.filter_for_service_consumer(user)
        return fields

    def validate(self, attrs):
        if attrs["role"].offering != attrs["resource"].offering:
            raise ValidationError(
                "Role and resource should belong to the same offering."
            )
        return attrs


class OfferingUserGroupDetailsSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    projects = structure_serializers.ProjectSerializer(many=True, read_only=True)

    class Meta:
        model = models.OfferingUserGroup
        fields = (
            "offering",
            "projects",
            "offering_uuid",
            "offering_name",
            "created",
            "modified",
            "backend_metadata",
        )
        extra_kwargs = dict(
            offering={
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        )


class OfferingUserGroupSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    projects = structure_serializers.ProjectSerializer(many=True)


def validate_plan(plan: models.Plan):
    """ "
    Ensure that maximum amount of resources with current plan is not reached yet.
    """
    if not plan.is_active:
        raise rf_exceptions.ValidationError(
            {"plan": _("Plan is not available because limit has been reached.")}
        )


def get_is_service_provider(serializer, scope) -> bool:
    customer = structure_permissions._get_customer(scope)
    return models.ServiceProvider.objects.filter(customer=customer).exists()


def add_service_provider(sender, fields, **kwargs):
    fields["is_service_provider"] = serializers.SerializerMethodField()
    setattr(sender, "get_is_service_provider", get_is_service_provider)


def add_service_provider_uuid(sender, fields, **kwargs):
    fields["service_provider_uuid"] = serializers.SlugRelatedField(
        slug_field="uuid",
        source="serviceprovider",
        read_only=True,
        allow_null=True,
    )


def add_service_provider_url(sender, fields, **kwargs):
    fields["service_provider"] = serializers.HyperlinkedRelatedField(
        lookup_field="uuid",
        view_name="marketplace-service-provider-detail",
        source="serviceprovider",
        read_only=True,
        allow_null=True,
    )


def get_call_managing_organization_uuid(serializer, scope) -> str | None:
    customer = structure_permissions._get_customer(scope)
    call_managing_organisation = (
        proposal_models.CallManagingOrganisation.objects.filter(customer=customer)
    )
    if call_managing_organisation.exists():
        return call_managing_organisation.first().uuid
    return None


def add_call_managing_organization_uuid(sender, fields, **kwargs):
    fields["call_managing_organization_uuid"] = serializers.SerializerMethodField()
    setattr(
        sender,
        "get_call_managing_organization_uuid",
        get_call_managing_organization_uuid,
    )


class ResourceTerminateSerializer(serializers.Serializer):
    attributes = serializers.JSONField(
        label=_("Termination attributes"), required=False
    )


class ResourceSetStateErredSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("error_message", "error_traceback")
        extra_kwargs = dict(
            error_message={"required": False},
            error_traceback={"required": False},
        )


class MoveResourceSerializer(serializers.Serializer):
    project = structure_serializers.NestedProjectSerializer(
        queryset=structure_models.Project.available_objects.all(),
        required=True,
        many=False,
    )


class ResourceSetLimitsSerializer(serializers.Serializer):
    limits = serializers.JSONField()

    class Meta:
        model = models.Resource
        fields = ("limits",)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_service_provider,
)

core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_service_provider_url,
)

core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_service_provider_uuid,
)

core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.CustomerSerializer,
    receiver=add_call_managing_organization_uuid,
)


def get_marketplace_resource_count(
    serializer, project: structure_models.Project
) -> dict[str, int]:
    counts = (
        models.Resource.objects.order_by()
        .exclude(state__in=(ResourceStates.TERMINATED,))
        .filter(
            project=project,
        )
        .values("offering__category__uuid")
        .annotate(count=Count("*"))
    )
    return {str(c["offering__category__uuid"]): c["count"] for c in list(counts)}


def add_marketplace_resource_count(sender, fields, **kwargs):
    fields["marketplace_resource_count"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_resource_count", get_marketplace_resource_count)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.ProjectSerializer,
    receiver=add_marketplace_resource_count,
)


class OfferingThumbnailSerializer(serializers.HyperlinkedModelSerializer):
    thumbnail = serializers.ImageField(required=True)

    class Meta:
        model = models.Offering
        fields = ("thumbnail",)


class OfferingImageSerializer(serializers.HyperlinkedModelSerializer):
    image = serializers.ImageField(required=True)

    class Meta:
        model = models.Offering
        fields = ("image",)


class OrganizationGroupsSerializer(serializers.Serializer):
    organization_groups = serializers.HyperlinkedRelatedField(
        queryset=structure_models.OrganizationGroup.objects.all(),
        view_name="organization-group-detail",
        lookup_field="uuid",
        required=False,
        many=True,
    )

    def save(self, **kwargs):
        if isinstance(self.instance, models.Offering):
            offering = self.instance
            organization_groups = self.validated_data["organization_groups"]
            offering.organization_groups.clear()

            if organization_groups:
                offering.organization_groups.add(*organization_groups)
        elif isinstance(self.instance, models.Plan):
            plan = self.instance
            organization_groups = self.validated_data["organization_groups"]
            plan.organization_groups.clear()

            if organization_groups:
                plan.organization_groups.add(*organization_groups)

        elif isinstance(self.instance, structure_models.Customer):
            customer = self.instance
            organization_groups = self.validated_data["organization_groups"]
            customer.organization_groups.clear()

            if organization_groups:
                customer.organization_groups.add(*organization_groups)


class ProviderOfferingCostsSerializer(serializers.Serializer):
    period = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    tax = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    def get_period(self, record) -> str:
        return "%s-%02d" % (record["invoice__year"], record["invoice__month"])

    def get_total(self, record) -> float:
        return round(record["computed_tax"] + record["computed_price"], 2)

    def get_price(self, record) -> float:
        return round(record["computed_price"], 2)

    def get_tax(self, record) -> float:
        return round(record["computed_tax"], 2)


class OfferingCostSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(source="resource__offering__uuid")
    cost = serializers.FloatField()


class OfferingComponentStatSerializer(serializers.Serializer):
    period = serializers.SerializerMethodField()
    billing_period = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    measured_unit = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()

    def get_date(self, record) -> str:
        date = parse_datetime(self.get_period(record))
        # for consistency with usage resource usage reporting, assume values at the beginning of the last day
        return (
            core_utils.month_end(date)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )

    def get_usage(self, record) -> int:
        return record["total_quantity"]

    def get_period(self, record) -> str:
        return "%s-%02d" % (record["invoice__year"], record["invoice__month"])

    def get_billing_period(self, record) -> str:
        return "%s-%02d-%02d" % (record["invoice__year"], record["invoice__month"], 1)

    def get_component_attr(self, record, attrname) -> str:
        component = self.context["offering_components_map"].get(
            record["details__offering_component_type"]
        )
        return component and getattr(component, attrname)

    def get_description(self, record) -> str:
        return self.get_component_attr(record, "description")

    def get_measured_unit(self, record) -> str:
        return self.get_component_attr(record, "measured_unit")

    def get_type(self, record) -> str:
        return self.get_component_attr(record, "type")

    def get_name(self, record) -> str:
        return self.get_component_attr(record, "name")


class CountStatsSerializer(serializers.Serializer):
    name = serializers.SerializerMethodField()
    uuid = serializers.SerializerMethodField()
    count = serializers.SerializerMethodField()

    def _get_value(self, record, name):
        for k in record.keys():
            if name in k:
                return record[k]

    def get_name(self, record) -> str:
        return self._get_value(record, "name")

    def get_uuid(self, record) -> str:
        return self._get_value(record, "uuid")

    def get_count(self, record) -> int:
        return self._get_value(record, "count")


class OfferingStatsCounterSerializer(serializers.Serializer):
    category_uuid = serializers.UUIDField()
    category_title = serializers.CharField()
    service_provider_name = serializers.CharField()
    service_provider_uuid = serializers.UUIDField()
    count = serializers.IntegerField()


class MarketplaceCustomerStatsSerializer(CountStatsSerializer):
    abbreviation = serializers.SerializerMethodField()

    def get_abbreviation(self, record) -> str:
        return self._get_value(record, "abbreviation")


class CustomerOecdCodeStatsSerializer(MarketplaceCustomerStatsSerializer):
    oecd = serializers.CharField(source="oecd_fos_2007_name")


class CustomerIndustryFlagStatsSerializer(MarketplaceCustomerStatsSerializer):
    is_industry = serializers.CharField()


class OfferingCountryStatsSerializer(serializers.Serializer):
    country = serializers.CharField(source="offering__country")
    count = serializers.IntegerField()


class ComponentUsagesStatsSerializer(serializers.Serializer):
    usage = serializers.DecimalField(decimal_places=2, max_digits=20)
    offering_uuid = serializers.UUIDField(source="resource__offering__uuid")
    component_type = serializers.CharField(source="component__type")


class ComponentUsagesPerMonthStatsSerializer(ComponentUsagesStatsSerializer):
    month = serializers.IntegerField(source="billing_period__month")
    year = serializers.IntegerField(source="billing_period__year")


class OfferingStatsSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    name = serializers.CharField(source="offering__name")
    uuid = serializers.CharField(source="offering__uuid")
    country = serializers.CharField(source="offering__country")


class MarketplaceProviderCustomerProjectSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = structure_models.Project
        fields = (
            "uuid",
            "name",
            "description",
            "end_date",
            "resources_count",
            "users_count",
            "billing_price_estimate",
        )

    resources_count = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()
    billing_price_estimate = serializers.SerializerMethodField()

    def get_resources(self, instance):
        service_provider = self.context["service_provider"]
        return utils.get_service_provider_resources(service_provider).filter(
            project=instance
        )

    def get_resources_count(self, instance) -> int:
        return self.get_resources(instance).count()

    def get_users_count(self, instance) -> int:
        return count_users(instance)

    @extend_schema_field(NestedPriceEstimateSerializer)
    def get_billing_price_estimate(self, instance):
        resources = self.get_resources(instance)
        return get_billing_price_estimate_for_resources(resources)


class ProviderProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = structure_models.Project
        fields = (
            "uuid",
            "name",
            "image",
        )


class ProviderUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "uuid",
            "full_name",
            "email",
            "image",
        )


class ProjectUserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    expiration_time = serializers.SerializerMethodField()
    offering_user_username = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "url",
            "uuid",
            "username",
            "full_name",
            "email",
            "role",
            "expiration_time",
            "offering_user_username",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

    def get_offering_user_username(self, user) -> str | None:
        offering = self.context["offering"]
        offering_user = models.OfferingUser.objects.filter(
            user=user, offering=offering
        ).first()
        return offering_user.username if offering_user else None

    def get_role(self, user: User) -> str:
        project = self.context["project"]
        permission = get_permissions(project, user).first()
        return permission and permission.role.name

    def get_expiration_time(self, user: User) -> datetime.datetime | None:
        project = self.context["project"]
        permission = get_permissions(project, user).first()
        return permission and permission.expiration_time


class MarketplaceServiceProviderUserSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = User
        fields = (
            "uuid",
            "username",
            "full_name",
            "first_name",
            "last_name",
            "organization",
            "email",
            "phone_number",
            "projects_count",
            "registration_method",
            "affiliations",
            "is_active",
        )

    projects_count = serializers.SerializerMethodField()

    def get_projects_count(self, user) -> int:
        service_provider = self.context["service_provider"]
        projects = utils.get_service_provider_project_ids(service_provider)
        content_type = ContentType.objects.get_for_model(structure_models.Project)
        return UserRole.objects.filter(
            user=user, object_id__in=projects, content_type=content_type, is_active=True
        ).count()

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if user.is_authenticated and not user.is_staff and not user.is_support:
            del fields["is_active"]

        return fields


class ProviderOfferingCustomerSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = structure_models.Customer
        fields = (
            "uuid",
            "name",
            "slug",
            "abbreviation",
            "phone_number",
            "email",
        )


class MarketplaceProviderCustomerSerializer(ProviderOfferingCustomerSerializer):
    class Meta:
        model = structure_models.Customer
        fields = ProviderOfferingCustomerSerializer.Meta.fields + (
            "uuid",
            "name",
            "slug",
            "abbreviation",
            "phone_number",
            "email",
            "payment_profiles",
            "billing_price_estimate",
            "projects_count",
            "users_count",
            "projects",
            "users",
        )

    payment_profiles = serializers.SerializerMethodField()
    billing_price_estimate = serializers.SerializerMethodField()
    projects_count = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()
    projects = serializers.SerializerMethodField()
    users = serializers.SerializerMethodField()

    def get_resources(self, customer):
        service_provider = self.context["service_provider"]
        return get_service_provider_resources(service_provider).filter(
            project__customer=customer
        )

    def get_users_qs(self, customer):
        service_provider = self.context["service_provider"]
        user = self.context["view"].request.user
        ids = get_service_provider_user_ids(user, service_provider, customer)
        return get_user_model().objects.filter(id__in=ids)

    @extend_schema_field(NestedPriceEstimateSerializer)
    def get_billing_price_estimate(self, customer):
        resources = self.get_resources(customer)
        return get_billing_price_estimate_for_resources(resources)

    @extend_schema_field(PaymentProfileSerializer(many=True))
    def get_payment_profiles(self, customer):
        return get_payment_profiles(self, customer)

    def get_projects_count(self, customer) -> int:
        return self.get_resources(customer).values_list("project_id").distinct().count()

    def get_users_count(self, customer) -> int:
        return self.get_users_qs(customer).count()

    @extend_schema_field(ProviderProjectSerializer(many=True))
    def get_projects(self, customer):
        resources = self.get_resources(customer)
        projects = structure_models.Project.available_objects.filter(
            id__in=resources.values_list("project_id")
        )[:5]
        serializer = ProviderProjectSerializer(
            instance=projects, many=True, context=self.context
        )
        return serializer.data

    @extend_schema_field(ProviderUserSerializer(many=True))
    def get_users(self, customer):
        users = self.get_users_qs(customer)[:5]
        serializer = ProviderUserSerializer(
            instance=users, many=True, context=self.context
        )
        return serializer.data


class ProviderOfferingSerializer(
    core_serializers.SlugSerializerMixin,
    core_serializers.RestrictedSerializerMixin,
    serializers.ModelSerializer,
):
    customer_uuid = serializers.UUIDField(read_only=True, source="customer.uuid")

    class Meta:
        model = models.Offering
        fields = (
            "uuid",
            "customer_uuid",
            "name",
            "slug",
            "category_title",
            "type",
            "state",
            "resources_count",
            "billing_price_estimate",
            "components",
            "plans",
            "options",
            "resource_options",
            "secret_options",
        )

    category_title = serializers.ReadOnlyField(source="category.title")
    resources_count = serializers.SerializerMethodField()
    billing_price_estimate = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    components = OfferingComponentSerializer(required=False, many=True)
    plans = BaseProviderPlanSerializer(many=True, required=False)
    secret_options = MergedSecretOptionsField(read_only=True)

    def get_state(
        self, offering: models.Offering
    ) -> Literal["Draft", "Active", "Paused", "Archived"]:
        return offering.get_state_display()

    def get_resources(self, offering: models.Offering):
        return models.Resource.objects.filter(offering=offering).exclude(
            state=ResourceStates.TERMINATED
        )

    def get_resources_count(self, offering: models.Offering) -> int:
        return self.get_resources(offering).count()

    @extend_schema_field(NestedPriceEstimateSerializer)
    def get_billing_price_estimate(self, offering: models.Offering):
        resources = self.get_resources(offering)
        return get_billing_price_estimate_for_resources(resources)

    def get_fields(self):
        fields = super().get_fields()
        if (
            self.instance
            and not self.can_see_secret_options()
            and "secret_options" in fields
        ):
            del fields["secret_options"]
        return fields

    def can_see_secret_options(self) -> bool:
        request = self.context.get("request")
        return request and permissions.can_see_secret_options(request, self.instance)


class FingerprintSerializer(serializers.Serializer):
    md5 = serializers.CharField(read_only=True)
    sha256 = serializers.CharField(read_only=True)
    sha512 = serializers.CharField(read_only=True)


class BaseServiceAccountSerializer(
    serializers.HyperlinkedModelSerializer, core_serializers.AugmentedSerializerMixin
):
    error_message = serializers.CharField(read_only=True)

    class Meta:
        model = models.BaseServiceAccount
        fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "username",
            "description",
            "error_message",
            "error_traceback",
        )
        read_only_fields = [
            "backend_id",
            "error_message",
            "error_traceback",
        ]


class BaseScopedServiceAccountSerializer(BaseServiceAccountSerializer):
    token = serializers.SerializerMethodField()
    expiresAt = serializers.SerializerMethodField()

    class Meta:
        model = models.ScopedServiceAccount
        fields = BaseServiceAccountSerializer.Meta.fields + (
            "token",
            "email",
            "expiresAt",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-service-account-detail",
            },
        }

    def get_token(self, obj) -> str | None:
        if hasattr(obj, "_token"):
            return obj._token
        return None

    def get_expiresAt(self, obj) -> str | None:
        if hasattr(obj, "_expiresAt"):
            return obj._expiresAt
        return None


class ProjectServiceAccountSerializer(BaseScopedServiceAccountSerializer):
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.available_objects.all(), slug_field="uuid"
    )
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.CharField(read_only=True, source="project.name")

    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    customer_name = serializers.CharField(
        read_only=True, source="project.customer.name"
    )
    customer_abbreviation = serializers.CharField(
        read_only=True, source="project.customer.abbreviation"
    )

    class Meta(BaseScopedServiceAccountSerializer.Meta):
        model = models.ProjectServiceAccount
        fields = BaseScopedServiceAccountSerializer.Meta.fields + (
            "project",
            "project_uuid",
            "project_name",
            "customer_uuid",
            "customer_name",
            "customer_abbreviation",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-project-service-account-detail",
            },
        }


class CustomerServiceAccountSerializer(BaseScopedServiceAccountSerializer):
    customer = serializers.SlugRelatedField(
        queryset=structure_models.Customer.objects.all(), slug_field="uuid"
    )
    customer_uuid = serializers.UUIDField(read_only=True, source="customer.uuid")
    customer_name = serializers.CharField(read_only=True, source="customer.name")

    class Meta(BaseScopedServiceAccountSerializer.Meta):
        model = models.CustomerServiceAccount
        fields = BaseScopedServiceAccountSerializer.Meta.fields + (
            "customer",
            "customer_uuid",
            "customer_name",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-customer-service-account-detail",
            },
        }


class RobotAccountSerializer(BaseServiceAccountSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="marketplace-robot-account-detail", lookup_field="uuid"
    )

    class Meta:
        model = models.RobotAccount
        fields = BaseServiceAccountSerializer.Meta.fields + (
            "resource",
            "type",
            "users",
            "keys",
            "backend_id",
            "fingerprints",
            "responsible_user",
            "state",
        )

        state = serializers.CharField(source="get_state_display", read_only=True)

        protected_fields = ["resource"]
        read_only_fields = BaseServiceAccountSerializer.Meta.read_only_fields + [
            "state",
        ]
        extra_kwargs = dict(
            resource={
                "lookup_field": "uuid",
                "view_name": "marketplace-resource-detail",
            },
            users={"lookup_field": "uuid", "view_name": "user-detail"},
            responsible_user={
                "lookup_field": "uuid",
                "view_name": "user-detail",
                "allow_null": True,
            },
        )

    fingerprints = serializers.SerializerMethodField()

    def get_state(self, robot_account: models.RobotAccount) -> str:
        return robot_account.get_state_display()

    @extend_schema_field(FingerprintSerializer(many=True))
    def get_fingerprints(self, robot_account):
        fingerprints = []
        for key in robot_account.keys:
            md5_fp, sha256fp, sha512_fp = get_ssh_key_fingerprints(key)
            fingerprints.append(
                {
                    "md5": md5_fp,
                    "sha256": sha256fp,
                    "sha512": sha512_fp,
                }
            )
        return fingerprints

    def validate_keys(self, keys):
        if not isinstance(keys, list):
            raise serializers.ValidationError(
                "JSON list of SSH public keys is expected."
            )
        for key in keys:
            validate_ssh_public_key(key)
        return keys

    def validate(self, validated_data):
        if self.instance:
            resource = self.instance.resource
        else:
            resource = validated_data["resource"]

        request = self.context["request"]
        if self.instance:
            permission = PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT
        else:
            permission = PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT

        if not has_permission(request, permission, resource.offering.customer):
            raise PermissionDenied()

        if "users" in validated_data:
            users = validated_data["users"]
        elif self.instance:
            users = self.instance.users.all()
        else:
            users = []

        resource_users = utils.get_resource_users(resource)
        if set(user.id for user in users) - set(user.id for user in resource_users):
            raise serializers.ValidationError(
                "User should belong to the same project or organization as resource."
            )

        responsible_user = validated_data.get("responsible_user")
        if responsible_user and responsible_user not in resource_users:
            raise serializers.ValidationError(
                "The responsible user should belong to the same project or organization as resource."
            )
        return validated_data


set_override(
    RobotAccountSerializer,
    "optional_fields",
    ["state", "error_message", "error_traceback"],
)


class StateTransitionErrorSerializer(serializers.Serializer):
    detail = serializers.CharField(
        help_text=_("Error message to be displayed to the user")
    )


class RobotAccountErrorSerializer(serializers.Serializer):
    error_message = serializers.CharField(
        required=False, help_text=_("Error message to be saved to the robot account")
    )


class RobotAccountDetailsSerializer(RobotAccountSerializer):
    users = structure_serializers.BasicUserSerializer(many=True, read_only=True)
    responsible_user = structure_serializers.BasicUserSerializer(
        read_only=True, allow_null=True
    )
    user_keys = serializers.SerializerMethodField()
    resource_uuid = serializers.UUIDField(read_only=True, source="resource.uuid")
    resource_name = serializers.CharField(read_only=True, source="resource.name")
    project_uuid = serializers.UUIDField(read_only=True, source="resource.project.uuid")
    project_name = serializers.CharField(read_only=True, source="resource.project.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="resource.project.customer.uuid"
    )
    customer_name = serializers.CharField(
        read_only=True, source="resource.project.customer.name"
    )
    offering_customer_uuid = serializers.UUIDField(
        read_only=True, source="resource.offering.customer.uuid"
    )
    offering_plugin_options = MergedPluginOptionsField(
        read_only=True, source="resource.offering.plugin_options"
    )

    class Meta(RobotAccountSerializer.Meta):
        fields = RobotAccountSerializer.Meta.fields + (
            "user_keys",
            "resource_name",
            "resource_uuid",
            "project_name",
            "project_uuid",
            "customer_uuid",
            "customer_name",
            "offering_customer_uuid",
            "offering_plugin_options",
        )

    @extend_schema_field(structure_serializers.SshKeySerializer(many=True))
    def get_user_keys(self, instance):
        return structure_serializers.SshKeySerializer(
            core_models.SshPublicKey.objects.filter(user__in=instance.users.all()),
            context=self.context,
            many=True,
        ).data


class ServiceProviderRevenues(serializers.Serializer):
    total = serializers.IntegerField(read_only=True)
    year = serializers.IntegerField(read_only=True, source="invoice__year")
    month = serializers.IntegerField(read_only=True, source="invoice__month")


class SectionSerializer(serializers.HyperlinkedModelSerializer):
    category_title = serializers.ReadOnlyField(source="category.title")

    class Meta:
        model = models.Section
        fields = (
            "url",
            "key",
            "created",
            "title",
            "category",
            "category_title",
            "is_standalone",
        )
        extra_kwargs = dict(
            category={
                "lookup_field": "uuid",
                "view_name": "marketplace-category-detail",
            },
            url={
                "lookup_field": "key",
                "view_name": "marketplace-section-detail",
            },
        )
        read_only_fields = ["created"]


class IntegrationStatusSerializer(serializers.ModelSerializer):
    status = serializers.CharField(read_only=True, source="get_status_display")
    agent_type = serializers.CharField(read_only=True, source="get_agent_type_display")

    class Meta:
        model = models.IntegrationStatus
        fields = (
            "agent_type",
            "status",
            "last_request_timestamp",
        )


class IntegrationStatusDetailsSerializer(
    serializers.HyperlinkedModelSerializer, IntegrationStatusSerializer
):
    class Meta:
        model = models.IntegrationStatus
        fields = (
            "agent_type",
            "status",
            "last_request_timestamp",
            "offering",
            "url",
        )
        protected_fields = ("offering",)
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-integration-status-detail",
            },
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        }


class ComponentUserUsageLimitSerializer(
    serializers.HyperlinkedModelSerializer, GetValueMixin
):
    component = serializers.SlugRelatedField(
        queryset=models.OfferingComponent.objects.all(),
        slug_field="uuid",
    )
    component_type = serializers.ReadOnlyField(source="component.type")

    class Meta:
        model = models.ComponentUserUsageLimit
        fields = (
            "url",
            "uuid",
            "resource",
            "component",
            "component_type",
            "user",
            "limit",
        )

        protected_fields = ("resource",)

        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "component-user-usage-limit-detail",
            },
            "resource": {
                "lookup_field": "uuid",
                "view_name": "marketplace-resource-detail",
            },
            "user": {
                "lookup_field": "uuid",
                "view_name": "marketplace-offering-user-detail",
            },
        }

    def validate_limit(self, limit):
        if limit < 0:
            raise serializers.ValidationError("Limit must be a positive number.")
        return limit

    def validate(self, attrs):
        component = self.get_from_attrs_or_instance(attrs, "component")
        resource = self.get_from_attrs_or_instance(attrs, "resource")
        offering_user = self.get_from_attrs_or_instance(attrs, "user")

        if not resource.project.has_user(offering_user.user):
            raise serializers.ValidationError(
                {"user": "The specified user is not part of the resource's project."}
            )

        if not self.instance:
            if not has_permission(
                self.context["request"],
                PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION,
                resource.project,
            ) and not has_permission(
                self.context["request"],
                PermissionEnum.RESOURCE_CONSUMPTION_LIMITATION,
                resource.project.customer,
            ):
                raise PermissionDenied()

        if not resource.offering.components.filter(uuid=component.uuid).exists():
            raise serializers.ValidationError({"component": "Component is wrong."})

        return attrs


@extend_schema_field(IntegrationStatusSerializer(many=True, allow_null=True))
def get_integration_status(serializer, offering):
    if not has_permission(
        serializer.context["request"], PermissionEnum.UPDATE_OFFERING, offering.customer
    ):
        return None

    statuses = models.IntegrationStatus.objects.filter(offering=offering)
    serializer = IntegrationStatusSerializer(instance=statuses, many=True)
    return serializer.data


def add_integration_status(sender, fields, **kwargs):
    fields["integration_status"] = serializers.SerializerMethodField()
    setattr(sender, "get_integration_status", get_integration_status)


core_signals.pre_serializer_fields.connect(
    sender=ProviderOfferingDetailsSerializer,
    receiver=add_integration_status,
)


class PluginComponentSerializer(serializers.Serializer):
    type = serializers.CharField()
    name = serializers.CharField()
    measured_unit = serializers.CharField()
    billing_type = serializers.ChoiceField(choices=BillingTypes.CHOICES)


class PluginOfferingTypeSerializer(serializers.Serializer):
    offering_type = serializers.CharField()
    components = PluginComponentSerializer(many=True)
    available_limits = serializers.ListField(child=serializers.CharField())


class ServiceProviderStatisticsSerializer(serializers.Serializer):
    active_campaigns = serializers.IntegerField(read_only=True)
    current_customers = serializers.IntegerField(read_only=True)
    customers_number_change = serializers.IntegerField(read_only=True)
    active_resources = serializers.IntegerField(read_only=True)
    resources_number_change = serializers.IntegerField(read_only=True)
    active_and_paused_offerings = serializers.IntegerField(read_only=True)
    unresolved_tickets = serializers.IntegerField(read_only=True)
    pending_orders = serializers.IntegerField(read_only=True)
    erred_resources = serializers.IntegerField(read_only=True)


class NameUUIDSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True)
    uuid = serializers.UUIDField(read_only=True)


class DetailStateSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True)
    state = serializers.CharField(read_only=True)


class RemoveOfferingComponentSerializer(serializers.Serializer):
    uuid = serializers.UUIDField()


class RuntimeStatesSerializer(serializers.Serializer):
    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)


class CustomerMemberCountSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    abbreviation = serializers.CharField(read_only=True)
    count = serializers.IntegerField(read_only=True)
    has_resources = serializers.BooleanField(read_only=True)


class SubresourceOfferingSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    type = serializers.CharField(read_only=True)


class ImportableResourceSerializer(serializers.Serializer):
    backend_id = serializers.CharField()
    name = serializers.CharField()
    type = serializers.CharField()
    description = serializers.CharField(allow_blank=True)


class OfferingReferenceSerializer(serializers.Serializer):
    offering_name = serializers.CharField(read_only=True)
    offering_uuid = serializers.UUIDField(read_only=True)


class OfferingGroupsSerializer(serializers.Serializer):
    customer_name = serializers.CharField(read_only=True)
    customer_uuid = serializers.CharField(read_only=True)
    offerings = OfferingReferenceSerializer(many=True, read_only=True)
