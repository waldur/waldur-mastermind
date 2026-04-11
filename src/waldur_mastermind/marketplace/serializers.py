import datetime
import logging
from decimal import Decimal
from typing import Literal, cast

import jwt
from constance import config
from dateutil.parser import parse as parse_datetime
from dateutil.relativedelta import relativedelta
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import DomainNameValidator
from django.db import transaction
from django.db.models import Count, Q, QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.drainage import set_override
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions as rf_exceptions
from rest_framework import serializers
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.permissions import SAFE_METHODS

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist import models as checklist_models
from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core import utils as core_utils
from waldur_core.core import validators as core_validators
from waldur_core.core.enums import CoreStates
from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.core.fields import NaturalChoiceField
from waldur_core.core.mixins import GetValueMixin
from waldur_core.core.models import NAME_LENGTH, User, get_ssh_key_fingerprints
from waldur_core.core.validators import BackendURLValidator, validate_ssh_public_key
from waldur_core.media.validators import ImageValidator
from waldur_core.permissions import models as permission_models
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
from waldur_core.structure.enums import ProjectKind
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
from waldur_mastermind.marketplace.billing_utils import convert_slurm_usage
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    SITE_AGENT_OFFERING,
    BillingTypes,
    CourseAccountState,
    LimitPeriods,
    OfferingStates,
    OfferingUserStates,
    OfferingUserStatesType,
    OrderStates,
    OrderStatesType,
    OrderTypes,
    ResourceAction,
    ResourceStates,
    ResourceStatesType,
    RobotAccountStates,
    ServiceAccountState,
    ServiceAccountStatesType,
)
from waldur_mastermind.marketplace.fields import PublicPlanField
from waldur_mastermind.marketplace.managers import ResourceQuerySet
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.processors import CreateResourceProcessor
from waldur_mastermind.marketplace.utils import (
    UsernameGenerationPolicy,
    check_pending_order_exists,
    get_service_provider_resources,
    get_service_provider_user_ids,
    parse_date,
    validate_attributes,
    validate_end_date,
    validate_limits,
)
from waldur_mastermind.marketplace_rancher.const import (
    DEPLOYMENT_MODE_MANAGED,
    DEPLOYMENT_MODE_SELF_MANAGED,
)
from waldur_mastermind.proposal import models as proposal_models
from waldur_pid import models as pid_models

from . import log, models, permissions, plugins, utils

logger = logging.getLogger(__name__)


class LifecyclePluginOptionsSerializer(serializers.Serializer):
    auto_approve_remote_orders = serializers.BooleanField(
        required=False,
        help_text="If set to True, an order can be processed without approval",
    )

    resource_expiration_threshold = serializers.IntegerField(
        required=False,
        default=30,
        help_text="Resource expiration threshold in days.",
    )

    service_provider_can_create_offering_user = serializers.BooleanField(
        required=False, help_text="Service provider can create offering user"
    )

    offering_user_auto_deletion = serializers.BooleanField(
        required=False,
        default=False,
        help_text="If set to True, offering users will be automatically marked "
        "for deletion by the cleanup task when users lose project access. "
        "If False (default), deletion must be triggered manually by the service provider.",
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
    latest_date_for_resource_termination = serializers.CharField(
        required=False,
        help_text="If set, it will be used as a latest date for resource termination. Format: YYYY-MM-DD",
    )
    auto_approve_in_service_provider_projects = serializers.BooleanField(
        required=False,
        help_text="Skip approval of public offering belonging to the same organization under which the request is done",
    )
    disable_autoapprove = serializers.BooleanField(
        required=False,
        help_text="If set to True, orders for this offering will always require manual approval, overriding auto_approve_in_service_provider_projects",
    )
    supports_downscaling = serializers.BooleanField(
        required=False,
        help_text="If set to True, it will be possible to downscale resources",
    )
    supports_pausing = serializers.BooleanField(
        required=False,
        help_text="If set to True, it will be possible to pause resources",
    )
    minimal_team_count_for_provisioning = serializers.IntegerField(
        required=False,
        help_text="Minimal team count required for provisioning of resources",
        min_value=1,
    )
    maximal_resource_count_per_project = serializers.IntegerField(
        required=False,
        help_text="Maximal number of offering resources allowed per project",
    )
    unique_resource_per_attribute = serializers.CharField(
        required=False,
        help_text="Attribute name to enforce uniqueness per value. "
        "E.g., 'storage_data_type' ensures only one resource per storage type per project.",
    )
    required_team_role_for_provisioning = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Required user role in a project for provisioning of resources",
    )
    enable_purchase_order_upload = serializers.BooleanField(
        required=False,
        help_text="If set to True, users will be able to upload purchase orders.",
    )
    require_purchase_order_upload = serializers.BooleanField(
        required=False,
        help_text="If set to True, users will be required to upload purchase orders.",
    )
    conceal_billing_data = serializers.BooleanField(
        required=False,
        help_text="If set to True, pricing and components tab would be concealed.",
    )
    create_orders_on_resource_option_change = serializers.BooleanField(
        required=False,
        help_text="If set to True, create orders when options of related resources are changed.",
    )
    can_restore_resource = serializers.BooleanField(
        required=False,
        help_text="If set to True, resource can be restored.",
    )
    enable_provider_consumer_messaging = serializers.BooleanField(
        required=False,
        help_text="If set to True, service providers can send messages with attachments to consumers on pending orders, and consumers can respond.",
    )
    notify_about_provider_consumer_messages = serializers.BooleanField(
        required=False,
        help_text="If set to True, send email notifications when providers or consumers exchange messages on pending orders.",
    )
    restrict_deletion_with_active_resources = serializers.BooleanField(
        required=False,
        help_text="If set to True, offering cannot be deleted while it has non-terminated resources.",
    )
    resource_name_pattern = serializers.CharField(
        required=False,
        help_text="Python format string for generating resource names. "
        "Available variables: {customer_name}, {customer_slug}, {project_name}, {project_slug}, "
        "{offering_name}, {offering_slug}, {plan_name}, {counter}, {attributes[KEY]}.",
    )

    def validate_latest_date_for_resource_termination(self, value):
        try:
            datetime.datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError):
            raise rf_exceptions.ValidationError(
                _("Invalid date format. Use YYYY-MM-DD.")
            )
        return value


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
    max_security_groups = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Default limit for number of security groups in OpenStack tenant",
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
    lbaas_enabled = serializers.BooleanField(
        required=False,
        help_text="If True, Octavia LBaaS (load balancers) is intended to be available for tenants from this offering.",
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
    scratch_project_directory = serializers.CharField(
        required=False, help_text="HEAppE scratch project directory"
    )
    project_permanent_directory = serializers.CharField(
        required=False, help_text="HEAppE project permanent directory"
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
    deployment_mode = serializers.ChoiceField(
        required=False,
        choices=[DEPLOYMENT_MODE_SELF_MANAGED, DEPLOYMENT_MODE_MANAGED],
        help_text="Rancher deployment mode",
    )
    flavors_regex = serializers.CharField(
        required=False, help_text="Regular expression to limit flavors list"
    )
    openstack_offering_uuid_list = serializers.ListSerializer(
        child=serializers.CharField(validators=[core_utils.validate_uuid]),
        required=False,
        help_text="List of UUID of OpenStack offerings where tenant can be created",
    )
    managed_rancher_server_flavor_name = serializers.CharField(
        required=False,
        help_text="Flavor name for managed Rancher server instances",
    )
    managed_rancher_server_system_volume_size_gb = serializers.IntegerField(
        required=False,
        help_text="System volume size in GB for managed Rancher server",
    )
    managed_rancher_server_system_volume_type_name = serializers.CharField(
        required=False,
        help_text="System volume type name for managed Rancher server",
    )
    managed_rancher_server_data_volume_size_gb = serializers.IntegerField(
        required=False,
        help_text="Data volume size in GB for managed Rancher server",
    )
    managed_rancher_server_data_volume_type_name = serializers.CharField(
        required=False,
        help_text="Data volume type name for managed Rancher server",
    )
    managed_rancher_worker_system_volume_size_gb = serializers.IntegerField(
        required=False,
        help_text="System volume size in GB for managed Rancher worker nodes",
    )
    managed_rancher_worker_system_volume_type_name = serializers.CharField(
        required=False,
        help_text="System volume type name for managed Rancher worker nodes",
    )
    managed_rancher_load_balancer_flavor_name = serializers.CharField(
        required=False,
        help_text="Flavor name for managed Rancher load balancer",
    )
    managed_rancher_load_balancer_system_volume_size_gb = serializers.IntegerField(
        required=False,
        help_text="System volume size in GB for managed Rancher load balancer",
    )
    managed_rancher_load_balancer_system_volume_type_name = serializers.CharField(
        required=False,
        help_text="System volume type name for managed Rancher load balancer",
    )
    managed_rancher_load_balancer_data_volume_size_gb = serializers.IntegerField(
        required=False,
        help_text="Data volume size in GB for managed Rancher load balancer",
    )
    managed_rancher_load_balancer_data_volume_type_name = serializers.CharField(
        required=False,
        help_text="Data volume type name for managed Rancher load balancer",
    )
    managed_rancher_tenant_max_cpu = serializers.IntegerField(
        help_text=_("Max number of vCPUs for tenants"),
        required=False,
    )
    managed_rancher_tenant_max_ram = serializers.IntegerField(
        help_text=_("Max number of RAM for tenants (GB)"),
        required=False,
    )
    managed_rancher_tenant_max_disk = serializers.IntegerField(
        help_text=_("Max size of disk space for tenants (GB)"),
        required=False,
    )


class AgentPluginOptionsSerializer(serializers.Serializer):
    account_name_generation_policy = serializers.ChoiceField(
        required=False,
        choices=[None, "project_slug"],
        help_text="Slurm account name generation policy",
        default=None,
        allow_null=True,
    )
    enable_display_of_order_actions_for_service_provider = serializers.BooleanField(
        required=False,
        help_text="Enable display of order actions for service provider",
        default=True,
    )
    slurm_periodic_policy_enabled = serializers.BooleanField(
        required=False,
        help_text="Enable SLURM periodic usage policy configuration. "
        "When enabled, allows configuring QoS-based threshold enforcement, "
        "carryover logic, and fairshare decay for site-agent managed SLURM offerings.",
        default=False,
    )


class OfferingResourceDisplayOptionsSerializer(serializers.Serializer):
    highlight_backend_id_display = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Defines if backend_id should be shown more prominently by the UI",
    )
    backend_id_display_label = serializers.CharField(
        required=False,
        default="Backend ID",
        allow_blank=True,
        help_text="Label used by UI for showing value of the backend_id",
    )
    disabled_resource_actions = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of disabled marketplace resource actions for this offering.",
    )

    def validate_disabled_resource_actions(self, value):
        valid_actions = [choice[0] for choice in ResourceAction.CHOICES]
        for action in value:
            if action not in valid_actions:
                raise rf_exceptions.ValidationError(
                    _("Invalid action: %s. Valid actions are: %s")
                    % (action, ", ".join(valid_actions))
                )
        return value


class ScriptPluginOptionsSerializer(serializers.Serializer):
    auto_approve_marketplace_script = serializers.BooleanField(
        required=False,
        default=True,
        help_text="If set to False, all orders require manual provider approval, including for service provider owners and staff",
    )


class KeycloakScopeOptionSerializer(serializers.Serializer):
    scope_type = serializers.CharField(
        help_text="Scope type, e.g. 'project', 'cluster'.",
    )
    scope_id = serializers.CharField(
        help_text="Identifier of the scope (UUID or external ID).",
    )
    label = serializers.CharField(
        help_text="Human-readable label shown to end users.",
    )


class KeycloakPluginOptionsSerializer(serializers.Serializer):
    keycloak_enabled = serializers.BooleanField(
        required=False,
        help_text="If set to True, Keycloak group management is enabled for this offering.",
    )
    keycloak_base_group = serializers.CharField(
        required=False,
        help_text="Root parent group in Keycloak under which offering groups are created. "
        "Groups are organized as: {base_group}/{offering_slug}/{role_group}. "
        "If empty, offering groups are created at the realm root.",
    )
    keycloak_sync_frequency = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="Frequency in minutes for syncing Keycloak group memberships.",
    )
    keycloak_group_name_template = serializers.CharField(
        required=False,
        help_text="Template for generating Keycloak group names. "
        "Uses $variable syntax (e.g. $offering_uuid_$role_name). "
        "Allowed variables: offering_uuid, offering_name, offering_slug, "
        "resource_uuid, resource_name, resource_slug, "
        "project_uuid, project_name, project_slug, "
        "organization_uuid, organization_name, organization_slug, "
        "role_name, scope_id.",
    )

    def validate_keycloak_group_name_template(self, value):
        if value:
            from waldur_keycloak.utils import validate_group_name_template

            error = validate_group_name_template(value)
            if error:
                raise serializers.ValidationError(error)
        return value

    keycloak_username_label = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        help_text="Custom label for the username field when inviting external users "
        "(e.g. 'Civil code', 'CUID'). If empty, defaults to 'Username'.",
    )


class ResourceKeycloakScopesSerializer(serializers.Serializer):
    keycloak_available_scopes = KeycloakScopeOptionSerializer(
        many=True,
        help_text="Pre-configured scope options for this resource.",
    )


class MergedPluginOptionsSerializer(
    LifecyclePluginOptionsSerializer,
    OpenStackPluginOptionsSerializer,
    HeappePluginOptionsSerializer,
    GLAuthPluginOptionsSerializer,
    SupportPluginOptionsSerializer,
    RancherPluginOptionsSerializer,
    AgentPluginOptionsSerializer,
    ScriptPluginOptionsSerializer,
    KeycloakPluginOptionsSerializer,
    OfferingResourceDisplayOptionsSerializer,
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
        help_text="TLS certificate for OpenStack API connection verification",
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


class RancherSecretOptionsSerializer(serializers.Serializer):
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

    cloud_init_template = serializers.CharField(
        required=False,
        help_text="Cloud-init template for Rancher cluster node initialization",
    )

    managed_rancher_load_balancer_cloud_init_template = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Cloud-init template for managed Rancher load balancer initialization",
    )

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
    RancherSecretOptionsSerializer,
):
    pass


@extend_schema_field(MergedPluginOptionsSerializer)
class MergedPluginOptionsField(serializers.JSONField):
    pass


@extend_schema_field(MergedSecretOptionsSerializer)
class MergedSecretOptionsField(serializers.JSONField):
    pass


class ReportSectionSerializer(serializers.Serializer):
    header = serializers.CharField(help_text="Section header text")
    body = serializers.CharField(help_text="Section body content")


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
            "allowed_domains",
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

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        request = self.context["request"]
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if request.user.is_anonymous:
            fields.pop("enable_notifications", None)

        if not request.user.is_staff:
            if "allowed_domains" in fields:
                fields["allowed_domains"].read_only = True

        return fields

    _validate_domain = DomainNameValidator()

    def validate_allowed_domains(self, value) -> list:
        if not isinstance(value, list):
            raise serializers.ValidationError(
                _("allowed_domains must be a list of domain names.")
            )
        for domain in value:
            if not isinstance(domain, str) or not domain.strip():
                raise serializers.ValidationError(
                    _("Error, domain entry must be a non-empty string.")
                )
            try:
                self._validate_domain(domain)
            except ValidationError:
                raise serializers.ValidationError(
                    _(
                        "'%(domain)s' is not a valid domain name. Please provide domains in format e.g. 'example.com' or 'api.provider.org'"
                    )
                    % {"domain": domain}
                )
        return value

    def validate(self, attrs):
        if not self.instance:
            permissions.can_register_service_provider(
                self.context["request"], attrs["customer"]
            )
        return attrs


class ServiceProviderApiSecretCodeSerializer(serializers.Serializer):
    api_secret_code = serializers.CharField(
        read_only=True,
        help_text="API secret code for authenticating service provider requests",
    )


class ServiceProviderComplianceOverviewSerializer(serializers.Serializer):
    """Serializer for service provider compliance statistics overview."""

    offering_uuid = serializers.UUIDField(read_only=True)
    offering_name = serializers.CharField(read_only=True)
    checklist_name = serializers.CharField(read_only=True, allow_null=True)
    total_users = serializers.IntegerField(read_only=True)
    users_with_completions = serializers.IntegerField(read_only=True)
    completed_users = serializers.IntegerField(read_only=True)
    pending_users = serializers.IntegerField(read_only=True)
    compliance_rate = serializers.FloatField(read_only=True)


class ServiceProviderChecklistSummarySerializer(serializers.Serializer):
    """Serializer for service provider checklist summary data."""

    checklist_uuid = serializers.UUIDField(read_only=True)
    checklist_name = serializers.CharField(read_only=True)
    questions_count = serializers.IntegerField(read_only=True)
    offerings_count = serializers.IntegerField(read_only=True)


class ServiceProviderOfferingUserComplianceSerializer(serializers.ModelSerializer):
    """Serializer for offering users with compliance status for service providers."""

    user_full_name = serializers.CharField(source="user.get_full_name", read_only=True)
    user_email = serializers.CharField(source="user.email", read_only=True)
    offering_name = serializers.CharField(source="offering.name", read_only=True)
    checklist_name = serializers.CharField(
        source="offering.compliance_checklist.name", read_only=True, allow_null=True
    )
    completion_percentage = serializers.SerializerMethodField()
    compliance_status = serializers.SerializerMethodField()
    last_updated = serializers.SerializerMethodField()

    class Meta:
        model = models.OfferingUser
        fields = (
            "uuid",
            "user_full_name",
            "user_email",
            "offering_name",
            "checklist_name",
            "username",
            "state",
            "completion_percentage",
            "compliance_status",
            "last_updated",
            "created",
        )
        read_only_fields = fields

    def _get_checklist_completion(self, obj):
        """
        Helper method to retrieve ChecklistCompletion for an OfferingUser.

        Returns:
            tuple: (checklist, completion) where:
                - checklist: The compliance checklist or None if not available
                - completion: ChecklistCompletion instance or None if not found
        """
        checklist = obj.offering.compliance_checklist
        if not checklist:
            return checklist, None

        try:
            content_type = ContentType.objects.get_for_model(obj)
            completion = checklist_models.ChecklistCompletion.objects.get(
                scope_content_type=content_type,
                scope_object_id=obj.id,
                checklist=checklist,
            )
            return checklist, completion
        except checklist_models.ChecklistCompletion.DoesNotExist:
            return checklist, None

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_completion_percentage(self, obj):
        """Get completion percentage for the offering user's checklist."""
        checklist, completion = self._get_checklist_completion(obj)

        if not checklist:
            return None
        if not completion:
            return 0

        return completion.get_completion_percentage()

    @extend_schema_field(serializers.CharField())
    def get_compliance_status(self, obj):
        """Get compliance status: completed, pending, or no_checklist."""
        checklist, completion = self._get_checklist_completion(obj)

        if not checklist:
            return "no_checklist"
        if not completion:
            return "pending"

        return "completed" if completion.is_completed else "pending"

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_last_updated(self, obj):
        """Get the last time the completion was updated."""
        checklist, completion = self._get_checklist_completion(obj)

        if not checklist or not completion:
            return None

        return completion.modified


class SetOfferingsUsernameSerializer(serializers.Serializer):
    user_uuid = serializers.UUIDField(help_text="UUID of the user")
    username = serializers.CharField(
        allow_blank=True, help_text="Username for offering access"
    )


class NestedAttributeOptionSerializer(serializers.ModelSerializer):
    is_default = serializers.SerializerMethodField()

    class Meta:
        model = models.AttributeOption
        fields = ("uuid", "key", "title", "is_default")

    def get_is_default(self, obj) -> bool:
        """Return True if this option is the default for its attribute."""
        return obj.attribute.default == obj.key


class NestedAttributeSerializer(serializers.ModelSerializer):
    options = NestedAttributeOptionSerializer(many=True)

    class Meta:
        model = models.Attribute
        fields = ("uuid", "key", "title", "type", "options", "required", "default")


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


class CategorySerializerForForNestedFields(
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.Category
        fields = ("url", "uuid", "title")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "marketplace-category-detail"}
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


class TagSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    offering_count = serializers.SerializerMethodField()
    created_by_username = serializers.ReadOnlyField(source="created_by.username")
    created_by_full_name = serializers.ReadOnlyField(source="created_by.full_name")

    class Meta:
        model = models.Tag
        fields = (
            "url",
            "uuid",
            "name",
            "description",
            "offering_count",
            "created",
            "created_by_username",
            "created_by_full_name",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "marketplace-tag-detail"},
        }

    def get_offering_count(self, tag) -> int:
        """
        Return offering count filtered by user permissions.
        Staff sees all offerings.
        Service providers see their own + active/paused/archived public offerings.
        """
        from waldur_core.structure.managers import get_connected_customers

        request = self.context.get("request")
        if not request:
            return 0

        user = request.user
        offerings = tag.offerings.all()

        # Staff and support see all
        if not user.is_anonymous and (user.is_staff or user.is_support):
            return offerings.count()

        if user.is_anonymous:
            return offerings.filter(state=models.Offering.States.ACTIVE).count()

        # Get connected customers for this user that have service providers
        connected_customers = get_connected_customers(user)
        user_customers = structure_models.Customer.objects.filter(
            id__in=connected_customers,
            serviceprovider__isnull=False,
        )

        # Filter: own offerings (any state) OR public visible states
        visible_states = [
            models.Offering.States.ACTIVE,
            models.Offering.States.PAUSED,
            models.Offering.States.ARCHIVED,
        ]

        return (
            offerings.filter(
                Q(customer__in=user_customers)  # Own offerings
                | Q(state__in=visible_states)  # Public visible offerings
            )
            .distinct()
            .count()
        )

    def create(self, validated_data):
        # Set created_by from request
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            validated_data["created_by"] = request.user
        return super().create(validated_data)


class NestedTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Tag
        fields = ("uuid", "name")


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
    quotas = serializers.DictField(
        child=serializers.IntegerField(min_value=0),
        help_text="Dictionary of quotas to update",
    )

    def save(self):
        new_quotas = self.validated_data["quotas"]
        new_keys = set(new_quotas.keys())
        plan: models.Plan = self.instance

        valid_types = {
            component.type
            for component in plan.offering.components.all()
            if component.billing_type == BillingTypes.FIXED
        }
        component_map = validate_components(new_keys, valid_types, plan)
        for key, old_component in component_map.items():
            new_amount = new_quotas.get(key, 0)
            if old_component.amount != new_amount:
                old_component.amount = new_amount
                old_component.save(update_fields=["amount"])


class DiscountConfigSerializer(serializers.Serializer):
    """Serializer for individual component discount configuration."""

    discount_threshold = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        help_text="Minimum quantity to be eligible for discount.",
    )
    discount_rate = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        max_value=100,
        help_text="Discount rate in percentage (0-100).",
    )

    def validate(self, attrs):
        """Ensure both threshold and rate are set together or both are None."""
        threshold = attrs.get("discount_threshold")
        rate = attrs.get("discount_rate")

        # If one is provided, both must be provided
        if (threshold is not None and rate is None) or (
            threshold is None and rate is not None
        ):
            raise serializers.ValidationError(
                "Both discount_threshold and discount_rate must be provided together, "
                "or both must be null to remove discount."
            )

        # If both are provided, validate they are reasonable
        if threshold is not None and rate is not None:
            if threshold <= 0:
                raise serializers.ValidationError(
                    "discount_threshold must be a positive number."
                )
            if rate < 0 or rate > 100:
                raise serializers.ValidationError(
                    "discount_rate must be between 0 and 100."
                )

        return attrs


class DiscountsUpdateSerializer(serializers.Serializer):
    """Serializer for updating discounts for multiple plan components."""

    discounts = serializers.DictField(
        child=DiscountConfigSerializer(),
        help_text="Dictionary mapping component types to their discount configuration.",
    )

    def validate_discounts(self, value):
        """Validate that component types exist in the plan's offering."""
        plan: models.Plan = self.instance

        new_keys = set(value.keys())
        valid_types = {component.type for component in plan.offering.components.all()}

        invalid_types = new_keys - valid_types
        if invalid_types:
            raise serializers.ValidationError(
                f"Invalid component types: {', '.join(invalid_types)}. "
                f"Valid types are: {', '.join(valid_types)}"
            )

        return value

    def save(self):
        """Apply discount configuration to plan components."""
        plan: models.Plan = self.instance
        discounts_config = self.validated_data["discounts"]

        updated_components = []

        for component_type, discount_data in discounts_config.items():
            try:
                plan_component = plan.components.get(component__type=component_type)
            except models.PlanComponent.DoesNotExist:
                logger.warning(
                    f"PlanComponent with type '{component_type}' not found in plan '{plan.uuid}'. "
                    "Skipping discount update for this component."
                )
                continue

            # Get the new values (could be None to clear discount)
            new_threshold = discount_data.get("discount_threshold")
            new_rate = discount_data.get("discount_rate")

            # Track if changes were made
            changed = False
            update_fields = []

            if plan_component.discount_threshold != new_threshold:
                plan_component.discount_threshold = new_threshold
                update_fields.append("discount_threshold")
                changed = True

            if plan_component.discount_rate != new_rate:
                plan_component.discount_rate = new_rate
                update_fields.append("discount_rate")
                changed = True

            if changed:
                plan_component.save(update_fields=update_fields)
                updated_components.append(
                    {
                        "component_type": component_type,
                        "discount_threshold": new_threshold,
                        "discount_rate": new_rate,
                    }
                )

                logger.info(
                    f"Updated discount for plan component '{component_type}' in plan '{plan.uuid}'. "
                    f"Threshold: {new_threshold}, Rate: {new_rate}%"
                )

        return updated_components


class NestedPlanComponentSerializer(serializers.ModelSerializer):
    type = serializers.ReadOnlyField(source="component.type")
    name = serializers.ReadOnlyField(source="component.name")
    measured_unit = serializers.ReadOnlyField(source="component.measured_unit")
    discounted_price = serializers.SerializerMethodField()
    discount_description = serializers.SerializerMethodField()

    class Meta:
        model = models.PlanComponent
        fields = (
            "type",
            "name",
            "measured_unit",
            "amount",
            "price",
            "future_price",
            "discount_threshold",
            "discount_rate",
            "discounted_price",
            "discount_description",
        )

    @extend_schema_field(
        serializers.DecimalField(max_digits=22, decimal_places=10, allow_null=True)
    )
    def get_discounted_price(self, component):
        if not component.discount_threshold or not component.discount_rate:
            return None
        return round(float(component.price) * (1 - component.discount_rate / 100), 10)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_discount_description(self, component):
        if not component.discount_threshold or not component.discount_rate:
            return None
        unit = component.component.measured_unit or ""
        return f"{component.discount_rate}% off when >= {component.discount_threshold} {unit}".strip()


class BasePlanSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    organization_groups = structure_serializers.OrganizationGroupSerializer(
        many=True, read_only=True
    )
    is_active = serializers.SerializerMethodField()
    description = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    components = NestedPlanComponentSerializer(many=True, read_only=True)

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
            "components",
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

    def get_prices(self, plan: models.Plan) -> dict[str, str]:
        return {item.component.type: item.price for item in plan.components.all()}

    def get_future_prices(self, plan: models.Plan) -> dict[str, str]:
        return {
            item.component.type: item.future_price for item in plan.components.all()
        }

    def get_quotas(self, plan: models.Plan) -> dict[str, int]:
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
            if BillingTypes.USAGE in components_types:
                plan_type = "usage-based"
            if BillingTypes.FIXED in components_types:
                plan_type = "fixed"
            if BillingTypes.ONE_TIME in components_types:
                plan_type = "one-time"
            if BillingTypes.ON_PLAN_SWITCH in components_types:
                plan_type = "on-plan-switch"
            if BillingTypes.LIMIT in components_types:
                plan_type = "limit"
        elif len(components_types) > 1:
            plan_type = "mixed"

        return plan_type

    def get_minimal_price(self, plan: models.Plan) -> str:
        price = 0

        components: QuerySet[models.PlanComponent] = plan.components.all()

        for plan_component in components:
            offering_component = plan_component.component

            if plan_component.price:
                if offering_component.billing_type == BillingTypes.LIMIT:
                    price += plan_component.price
                elif offering_component.billing_type == BillingTypes.FIXED:
                    price += plan_component.price * (plan_component.amount or 1)
                elif offering_component.billing_type == BillingTypes.ONE_TIME:
                    price += plan_component.price

        return price


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
    offering_uuid = serializers.UUIDField(
        required=False, help_text="UUID of the offering"
    )
    customer_provider_uuid = serializers.UUIDField(
        required=False, help_text="UUID of the customer provider"
    )
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
        help_text="Order by field",
    )


class PlanUsageResponseSerializer(serializers.Serializer):
    plan_uuid = serializers.UUIDField(
        read_only=True, source="uuid", help_text="UUID of the plan"
    )
    plan_name = serializers.CharField(
        read_only=True, source="name", help_text="Name of the plan"
    )

    limit = serializers.IntegerField(read_only=True, help_text="Usage limit")
    usage = serializers.IntegerField(read_only=True, help_text="Current usage count")
    remaining = serializers.IntegerField(read_only=True, help_text="Remaining usage")

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
    "conditional_cascade",
    "component_multiplier",
    "single_datacenter_k8s_config",
    "multi_datacenter_k8s_config",
    "storage_folder_manager",
)

# Storage folder permission choices - exported via OpenAPI as StorageFolderPermissionEnum
STORAGE_FOLDER_PERMISSIONS = (
    ("2770", "2770 - Group write, setgid (recommended for shared projects)"),
    ("2775", "2775 - Group write, world read, setgid"),
    ("2777", "2777 - Full access, setgid (least secure)"),
    ("770", "770 - Group write, no setgid"),
    ("775", "775 - Group write, world read, no setgid"),
    ("777", "777 - Full access, no setgid"),
)


class CascadeStepSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()
    type = serializers.ChoiceField(choices=["select_string", "select_string_multi"])
    depends_on = serializers.CharField(required=False)
    choices = serializers.JSONField(required=False)  # JSON string or parsed data
    choices_map = serializers.JSONField(required=False)  # JSON string or parsed data

    def validate(self, attrs):
        import json

        errors = {}

        # Parse and validate choices if provided
        if attrs.get("choices"):
            choices_raw = attrs["choices"]
            # Handle both JSON string and already parsed data
            if isinstance(choices_raw, str):
                try:
                    choices_data = json.loads(choices_raw)
                except json.JSONDecodeError:
                    errors["choices"] = "choices must be valid JSON"
                    choices_data = None
            else:
                # Already parsed (from JSONField)
                choices_data = choices_raw

            if choices_data is not None:
                if not isinstance(choices_data, list):
                    errors["choices"] = "choices must be a JSON array"
                else:
                    for i, choice in enumerate(choices_data):
                        if (
                            not isinstance(choice, dict)
                            or "value" not in choice
                            or "label" not in choice
                        ):
                            errors["choices"] = (
                                f"Choice {i + 1} must be an object with 'value' and 'label' properties"
                            )
                            break
                        # Convert value to string for JSON serialization consistency
                        choice["value"] = str(choice["value"])
                    if "choices" not in errors:
                        attrs["choices"] = choices_data

        # Parse and validate choices_map if provided
        if attrs.get("choices_map"):
            choices_map_raw = attrs["choices_map"]
            # Handle both JSON string and already parsed data
            if isinstance(choices_map_raw, str):
                try:
                    choices_map_data = json.loads(choices_map_raw)
                except json.JSONDecodeError:
                    errors["choices_map"] = "choices_map must be valid JSON"
                    choices_map_data = None
            else:
                # Already parsed (from JSONField)
                choices_map_data = choices_map_raw

            if choices_map_data is not None:
                if not isinstance(choices_map_data, dict):
                    errors["choices_map"] = (
                        'choices_map must be a JSON object mapping parent values to choice arrays, e.g. {"parent1": [{"value": "child1", "label": "Child 1"}]}'
                    )
                else:
                    for key, value in choices_map_data.items():
                        if not isinstance(value, list):
                            errors["choices_map"] = (
                                f"choices_map['{key}'] must be an array"
                            )
                            break
                        for j, choice in enumerate(value):
                            if (
                                not isinstance(choice, dict)
                                or "value" not in choice
                                or "label" not in choice
                            ):
                                errors["choices_map"] = (
                                    f"Choice {j + 1} in choices_map['{key}'] must be an object with 'value' and 'label' properties"
                                )
                                break
                            # Convert value to string for JSON serialization consistency
                            choice["value"] = str(choice["value"])
                        if "choices_map" in errors:
                            break
                    if "choices_map" not in errors:
                        attrs["choices_map"] = choices_map_data

        # Validate required fields
        if attrs.get("depends_on") and not attrs.get("choices_map"):
            errors["choices_map"] = (
                "choices_map is required when depends_on is specified"
            )
        if not attrs.get("depends_on") and not attrs.get("choices"):
            errors["choices"] = "choices is required when depends_on is not specified"

        if errors:
            raise serializers.ValidationError(errors)

        return attrs


class StringKeyListField(serializers.ListField):
    """ListField that converts integer error keys to strings for JSON serialization"""

    def run_validation(self, data=serializers.empty):
        try:
            return super().run_validation(data)
        except serializers.ValidationError as exc:
            # Convert any integer keys in error details to strings
            if hasattr(exc, "detail") and isinstance(exc.detail, dict):
                detail = {}
                for key, value in exc.detail.items():
                    detail[str(key)] = value
                raise serializers.ValidationError(detail)
            raise


class CascadeConfigSerializer(serializers.Serializer):
    steps = StringKeyListField(child=CascadeStepSerializer())

    def validate(self, attrs):
        steps = attrs.get("steps", [])

        if not steps:
            raise serializers.ValidationError(
                {"steps": "At least one step is required"}
            )

        # Collect all validation errors for better error reporting
        errors = {}

        step_names = []
        for i, step in enumerate(steps):
            step_name = step.get("name")
            if step_name:
                step_names.append(step_name)

        # Check for unique step names
        if len(step_names) != len(set(step_names)):
            errors["steps"] = "Step names must be unique"

        # Validate dependencies exist and are not circular
        for i, step in enumerate(steps):
            depends_on = step.get("depends_on")
            if depends_on:
                # Check if dependency exists in previous steps
                if depends_on not in [
                    s.get("name") for s in steps[:i] if s.get("name")
                ]:
                    errors["steps"] = (
                        f"Step '{step.get('name', f'step_{i}')}' depends on '{depends_on}' which must be defined earlier"
                    )
                    break

        if errors:
            raise serializers.ValidationError(errors)

        return attrs


class K8sDefaultConfigurationSerializer(serializers.Serializer):
    """Serializer for Kubernetes cluster default configuration options"""

    # Controller node defaults
    default_controller_vcpus = serializers.IntegerField(
        min_value=1, max_value=64, required=False
    )
    default_controller_ram_gb = serializers.IntegerField(
        min_value=1, max_value=128, required=False
    )
    default_controller_system_disk_gb = serializers.IntegerField(
        min_value=1, max_value=500, required=False
    )
    default_controller_etcd_disk_gb = serializers.IntegerField(
        min_value=1, max_value=1000, required=False
    )

    # Load balancer defaults
    default_lb_vcpus = serializers.IntegerField(
        min_value=1, max_value=32, required=False
    )
    default_lb_ram_gb = serializers.IntegerField(
        min_value=1, max_value=256, required=False
    )
    default_lb_system_disk_gb = serializers.IntegerField(
        min_value=1, max_value=500, required=False
    )
    default_lb_logs_disk_gb = serializers.IntegerField(
        min_value=1, max_value=2000, required=False
    )

    # Worker node requirements
    minimal_worker_vcpus = serializers.IntegerField(
        min_value=1, max_value=32, required=False
    )
    minimal_worker_ram_gb = serializers.IntegerField(
        min_value=1, max_value=64, required=False
    )

    # Volume defaults
    default_worker_data_disk_gb = serializers.IntegerField(
        min_value=1, max_value=10000, required=False
    )
    default_storage_data_disk_gb = serializers.IntegerField(
        min_value=1, max_value=10000, required=False
    )
    default_storage_san_disk_gb = serializers.IntegerField(
        min_value=1, max_value=50000, required=False
    )

    # Configuration options
    available_kubernetes_versions = serializers.CharField(
        required=False,
        help_text="Comma-separated list of Kubernetes versions (e.g., 1.32.0,1.33.0,1.34.0)",
        allow_blank=True,
    )

    def validate_available_kubernetes_versions(self, value):
        """Validate Kubernetes version format"""
        if not value or not value.strip():
            return value

        versions = [v.strip() for v in value.split(",") if v.strip()]
        if not versions:
            return value

        # Validate version format (x.y.z)
        import re

        version_pattern = re.compile(r"^\d+\.\d+\.\d+$")
        invalid_versions = [v for v in versions if not version_pattern.match(v)]

        if invalid_versions:
            raise serializers.ValidationError(
                {
                    "available_kubernetes_versions": f"Invalid Kubernetes version format(s): {', '.join(invalid_versions)}. Expected format: x.y.z"
                }
            )

        return value


class ComponentMultiplierConfigSerializer(serializers.Serializer):
    component_type = serializers.CharField()
    factor = serializers.IntegerField(min_value=1)
    min_limit = serializers.IntegerField(min_value=0, required=False)
    max_limit = serializers.IntegerField(min_value=0, required=False)

    def validate(self, attrs):
        min_limit = attrs.get("min_limit")
        max_limit = attrs.get("max_limit")

        if min_limit is not None and max_limit is not None and min_limit > max_limit:
            raise serializers.ValidationError(
                "min_limit cannot be greater than max_limit"
            )

        return attrs


class StorageDataTypeSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()


class StorageFolderConfigSerializer(serializers.Serializer):
    component_type = serializers.CharField(required=True)
    default_hard_quota_multiplier = serializers.FloatField(default=1.0, min_value=1.0)
    inode_soft_multiplier = serializers.IntegerField(default=7000, min_value=1)
    inode_hard_multiplier = serializers.IntegerField(default=10000, min_value=1)
    storage_data_types = StorageDataTypeSerializer(many=True, required=True)
    default_permission = serializers.ChoiceField(
        choices=STORAGE_FOLDER_PERMISSIONS,
        default="2770",
        help_text="Default permission to auto-select",
    )

    def validate_component_type(self, value):
        # Component type validation will be handled at the offering level
        return value

    def validate(self, attrs):
        storage_data_types = attrs.get("storage_data_types", [])

        if not storage_data_types:
            raise serializers.ValidationError(
                {"storage_data_types": "At least one storage data type is required"}
            )

        # Validate unique keys for storage data types
        data_type_keys = [dt.get("key") for dt in storage_data_types if dt.get("key")]
        if len(data_type_keys) != len(set(data_type_keys)):
            raise serializers.ValidationError(
                {"storage_data_types": "Storage data type keys must be unique"}
            )

        # Validate inode multipliers
        soft_multiplier = attrs.get("inode_soft_multiplier")
        hard_multiplier = attrs.get("inode_hard_multiplier")
        if soft_multiplier and hard_multiplier and hard_multiplier < soft_multiplier:
            raise serializers.ValidationError(
                {
                    "inode_hard_multiplier": "Hard inode multiplier cannot be less than soft inode multiplier"
                }
            )

        return attrs


class OptionValidatorSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["gt", "gte", "lt", "lte"])
    target_field = serializers.CharField()


class OptionFieldSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=FIELD_TYPES)
    label = serializers.CharField()
    help_text = serializers.CharField(required=False)
    required = serializers.BooleanField(default=False)
    choices = serializers.ListField(child=serializers.CharField(), required=False)
    default = serializers.CharField(required=False)
    min = serializers.IntegerField(required=False)
    max = serializers.IntegerField(required=False)
    cascade_config = CascadeConfigSerializer(required=False)
    component_multiplier_config = ComponentMultiplierConfigSerializer(required=False)
    storage_folder_config = StorageFolderConfigSerializer(required=False)
    default_configs = K8sDefaultConfigurationSerializer(required=False)
    validators = serializers.ListField(
        child=OptionValidatorSerializer(), required=False
    )

    def validate(self, attrs):
        field_type = attrs.get("type")

        if field_type == "conditional_cascade":
            if not attrs.get("cascade_config"):
                raise serializers.ValidationError(
                    "cascade_config is required for conditional_cascade type"
                )

        if field_type == "component_multiplier":
            if not attrs.get("component_multiplier_config"):
                raise serializers.ValidationError(
                    "component_multiplier_config is required for component_multiplier type"
                )

        if field_type == "storage_folder_manager":
            if not attrs.get("storage_folder_config"):
                raise serializers.ValidationError(
                    "storage_folder_config is required for storage_folder_manager type"
                )

        if field_type in (
            "single_datacenter_k8s_config",
            "multi_datacenter_k8s_config",
        ):
            # default_configs is optional for K8s config types
            # When not provided, the frontend will show configuration warnings
            pass

        return attrs


class OfferingOptionsSerializer(serializers.Serializer):
    order = serializers.ListField(child=serializers.CharField())
    options = serializers.DictField(child=OptionFieldSerializer())

    def validate(self, attrs):
        options = attrs.get("options", {})
        for name, option in options.items():
            validators = option.get("validators")
            if not validators:
                continue

            for validator in validators:
                target_field = validator.get("target_field")
                if target_field:
                    if target_field not in options:
                        raise serializers.ValidationError(
                            {
                                "options": _(
                                    "Target field %(target)s for option %(name)s not found in options."
                                )
                                % {"target": target_field, "name": name}
                            }
                        )

                    target_type = options[target_field].get("type")
                    if target_type not in ["integer", "money"]:
                        raise serializers.ValidationError(
                            {
                                "options": _(
                                    "Target field %(target)s for option %(name)s must be integer or money."
                                )
                                % {"target": target_field, "name": name}
                            }
                        )
        return attrs


class OfferingComponentSerializer(serializers.ModelSerializer):
    factor = serializers.SerializerMethodField()
    overage_component = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.OfferingComponent.objects.all(),
        required=False,
        allow_null=True,
    )

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
            "is_prepaid",
            "overage_component",
            "min_prepaid_duration",
            "max_prepaid_duration",
            "prepaid_duration_step",
            "min_renewal_duration",
            "max_renewal_duration",
            "renewal_duration_step",
        )
        extra_kwargs = {
            "billing_type": {"required": True},
        }

    def validate(self, attrs):
        if attrs.get("is_boolean"):
            attrs["min_value"] = 0
            attrs["max_value"] = 1
            attrs["limit_period"] = LimitPeriods.MONTH
            attrs["limit_amount"] = None
        if (
            self.instance
            and self.instance.offering.type == OPENSTACK_TENANT_OFFERING
            and self.instance.is_builtin
        ):
            protected_fields = set(attrs.keys()) & {
                "type",
                "name",
                "measured_unit",
            }
            if protected_fields:
                raise serializers.ValidationError(
                    "Built-in OpenStack offering component type, name and unit are not editable."
                )
        self._validate_prepaid(attrs)

        return attrs

    def _validate_prepaid(self, attrs):
        # Determine the final state of 'is_prepaid'.
        # On update, if 'is_prepaid' is not in the request, use the existing value.
        # On create, if not provided, it defaults to False.
        if self.instance:
            is_prepaid = attrs.get("is_prepaid", self.instance.is_prepaid)
        else:
            is_prepaid = attrs.get("is_prepaid", False)

        overage_component = cast(
            models.OfferingComponent | None, attrs.get("overage_component")
        )

        if not is_prepaid:
            # Clear renewal/prepaid duration constraints if the component is not prepaid.
            for field in (
                "min_prepaid_duration",
                "max_prepaid_duration",
                "prepaid_duration_step",
                "min_renewal_duration",
                "max_renewal_duration",
                "renewal_duration_step",
            ):
                if field in attrs:
                    raise serializers.ValidationError(
                        {field: _("This field can only be set on prepaid components.")}
                    )

        # Cross-field validation for prepaid duration range
        min_prepaid = attrs.get("min_prepaid_duration")
        max_prepaid = attrs.get("max_prepaid_duration")
        if (
            min_prepaid is not None
            and max_prepaid is not None
            and min_prepaid > max_prepaid
        ):
            raise serializers.ValidationError(
                {
                    "min_prepaid_duration": _(
                        "Minimum prepaid duration must not exceed the maximum."
                    )
                }
            )

        # Cross-field validation for renewal duration range
        min_renewal = attrs.get("min_renewal_duration")
        max_renewal = attrs.get("max_renewal_duration")
        if (
            min_renewal is not None
            and max_renewal is not None
            and min_renewal > max_renewal
        ):
            raise serializers.ValidationError(
                {
                    "min_renewal_duration": _(
                        "Minimum renewal duration must not exceed the maximum."
                    )
                }
            )

        if overage_component:
            # Rule 1: The current component must be prepaid to have an overage component.
            if not is_prepaid:
                raise serializers.ValidationError(
                    {
                        "overage_component": _(
                            "An overage component can only be specified for prepaid components. "
                            "Please set 'is_prepaid' to true or remove the overage component."
                        )
                    }
                )

            # Rule 2: The linked overage component itself cannot be a prepaid component.
            if overage_component.is_prepaid:
                raise serializers.ValidationError(
                    {
                        "overage_component": _(
                            "The linked overage component cannot be a prepaid component itself."
                        )
                    }
                )

            # Rule 3: The overage component's billing type must be USAGE
            if overage_component.billing_type != BillingTypes.USAGE:
                raise serializers.ValidationError(
                    {
                        "overage_component": _(
                            "The linked overage component must have a billing type of 'usage'."
                        )
                    }
                )

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

    def get_factor(self, offering_component: models.OfferingComponent) -> int | None:
        builtin_components = plugins.manager.get_components(
            offering_component.offering.type
        )
        for c in builtin_components:
            if c.type == offering_component.type:
                return c.factor


# Used only for OpenAPI schema generation
class UpdateOfferingComponent(OfferingComponentSerializer):
    uuid = serializers.UUIDField()

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # Check for type uniqueness within the offering when type is being updated
        if "type" in attrs and self.instance:
            new_type = attrs["type"]
            current_type = self.instance.type

            # Only check uniqueness if type is actually changing
            if new_type != current_type:
                offering = self.instance.offering
                existing_component = (
                    offering.components.filter(type=new_type)
                    .exclude(uuid=self.instance.uuid)
                    .first()
                )

                if existing_component:
                    raise serializers.ValidationError(
                        {
                            "type": f"Component with type '{new_type}' already exists in this offering."
                        }
                    )

        return attrs


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


class UserAuthMethodCountSerializer(serializers.Serializer):
    method = serializers.CharField(help_text="Authentication method")
    count = serializers.IntegerField(help_text="Number of users")


class UserIdentitySourceCountSerializer(serializers.Serializer):
    identity_source = serializers.CharField(help_text="Identity source")
    count = serializers.IntegerField(help_text="Number of users")


class UserOrganizationCountSerializer(serializers.Serializer):
    organization = serializers.CharField(help_text="Organization name")
    count = serializers.IntegerField(help_text="Number of users")


class UserAffiliationCountSerializer(serializers.Serializer):
    affiliation = serializers.CharField(help_text="Affiliation name")
    count = serializers.IntegerField(help_text="Number of users")


class UserOrganizationTypeCountSerializer(serializers.Serializer):
    organization_type = serializers.CharField(
        help_text="Organization type (SCHAC URN)", allow_null=True
    )
    count = serializers.IntegerField(help_text="Number of users")


class UserJobTitleCountSerializer(serializers.Serializer):
    job_title = serializers.CharField(help_text="Job title", allow_null=True)
    count = serializers.IntegerField(help_text="Number of users")


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
    offering_uuid = serializers.ReadOnlyField(source="plan.offering.uuid")
    offering_name = serializers.ReadOnlyField(source="plan.offering.name")
    plan_uuid = serializers.ReadOnlyField(source="plan.uuid")
    plan_name = serializers.ReadOnlyField(source="plan.name")
    plan_unit = serializers.ReadOnlyField(source="plan.unit")
    component_name = serializers.ReadOnlyField(source="component.name")
    measured_unit = serializers.ReadOnlyField(source="component.measured_unit")
    billing_type = serializers.ReadOnlyField(source="component.billing_type")

    class Meta:
        model = models.PlanComponent
        fields = (
            "offering_uuid",
            "offering_name",
            "plan_uuid",
            "plan_name",
            "plan_unit",
            "component_name",
            "measured_unit",
            "billing_type",
            "amount",
            "price",
            "future_price",
            "discount_threshold",
            "discount_rate",
        )


class NestedEndpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.OfferingAccessEndpoint
        fields = ("uuid", "name", "url")

    url = serializers.CharField(
        validators=[core_validators.BackendURLValidator],
        help_text="URL of the access endpoint",
    )


class EndpointUUIDSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(help_text="UUID of the access endpoint")


class SoftwareCatalogUUIDSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(help_text="UUID of the software catalog")


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


class CatalogSummarySerializer(serializers.ModelSerializer):
    """Summary serializer for SoftwareCatalog used in nested context."""

    class Meta:
        model = models.SoftwareCatalog
        fields = ("uuid", "name", "version", "description")


class PartitionSummarySerializer(serializers.ModelSerializer):
    """Summary serializer for OfferingPartition used in nested context."""

    class Meta:
        model = models.OfferingPartition
        fields = (
            "uuid",
            "partition_name",
            "priority_tier",
            "qos",
            "cpu_arch",
            "gpu_arch",
        )


class NestedSoftwareCatalogSerializer(serializers.ModelSerializer):
    catalog = CatalogSummarySerializer(read_only=True)
    partition = PartitionSummarySerializer(read_only=True, allow_null=True)
    package_count = serializers.SerializerMethodField()

    class Meta:
        model = models.OfferingSoftwareCatalog
        fields = (
            "uuid",
            "catalog",
            "enabled_cpu_family",
            "enabled_cpu_microarchitectures",
            "package_count",
            "partition",
        )

    @extend_schema_field(serializers.IntegerField())
    def get_package_count(self, obj):
        """Get total number of packages in this catalog."""
        return obj.catalog.packages.count()


class NestedPartitionSerializer(serializers.ModelSerializer):
    """Nested serializer for OfferingPartition model."""

    class Meta:
        model = models.OfferingPartition
        fields = (
            "uuid",
            "partition_name",
            "cpu_arch",
            "gpu_arch",
            "cpu_bind",
            "def_cpu_per_gpu",
            "max_cpus_per_node",
            "max_cpus_per_socket",
            "def_mem_per_cpu",
            "def_mem_per_gpu",
            "def_mem_per_node",
            "max_mem_per_cpu",
            "max_mem_per_node",
            "default_time",
            "max_time",
            "grace_time",
            "max_nodes",
            "min_nodes",
            "exclusive_topo",
            "exclusive_user",
            "priority_tier",
            "qos",
            "req_resv",
        )


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
    scope_name = serializers.CharField(
        read_only=True, source="scope.name", allow_null=True
    )
    scope_state = serializers.SerializerMethodField()
    scope_error_message = serializers.SerializerMethodField()
    files = NestedOfferingFileSerializer(many=True, read_only=True)
    quotas = serializers.SerializerMethodField()
    organization_groups = structure_serializers.OrganizationGroupSerializer(
        many=True, read_only=True
    )
    tags = NestedTagSerializer(many=True, read_only=True)
    total_customers = serializers.SerializerMethodField()
    total_cost = serializers.SerializerMethodField()
    total_cost_estimated = serializers.SerializerMethodField()
    endpoints = NestedEndpointSerializer(many=True, read_only=True)
    software_catalogs = NestedSoftwareCatalogSerializer(many=True, read_only=True)
    partitions = NestedPartitionSerializer(many=True, read_only=True)
    roles = NestedRoleSerializer(many=True, read_only=True)
    has_compliance_requirements = serializers.SerializerMethodField()
    billing_type_classification = serializers.SerializerMethodField()
    effective_available_limits = serializers.SerializerMethodField()
    compliance_checklist = serializers.HyperlinkedRelatedField(
        queryset=checklist_models.Checklist.objects.filter(
            checklist_type=checklist_enums.ChecklistTypes.OFFERING_COMPLIANCE
        ),
        view_name="checklists-admin-detail",
        lookup_field="uuid",
        required=False,
        allow_null=True,
    )

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
            "privacy_policy_link",
            "helpdesk_url",
            "documentation_url",
            "access_url",
            "endpoints",
            "software_catalogs",
            "partitions",
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
            "backend_id_rules",
            "organization_groups",
            "tags",
            "image",
            "total_customers",
            "total_cost",
            "total_cost_estimated",
            "parent_description",
            "parent_uuid",
            "parent_name",
            "backend_metadata",
            "has_compliance_requirements",
            "billing_type_classification",
            "effective_available_limits",
            "compliance_checklist",
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
            fields.pop("secret_options", None)
            fields.pop("service_attributes", None)
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
    ) -> Literal["Draft", "Active", "Paused", "Archived", "Unavailable"]:
        return offering.get_state_display()

    @extend_schema_field(
        serializers.ChoiceField(choices=CoreStates.labels, allow_null=True)
    )
    def get_scope_state(self, offering: models.Offering):
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

    @extend_schema_field(serializers.BooleanField())
    def get_has_compliance_requirements(self, offering: models.Offering) -> bool:
        """Quick check if this offering requires compliance."""
        return offering.compliance_checklist is not None

    def get_billing_type_classification(self, offering: models.Offering) -> str:
        """
        Classify offering components by billing type.
        Returns 'limit_only', 'usage_only', or 'mixed'.
        """
        components = offering.components.all()
        if not components.exists():
            return "mixed"

        billing_types = set(component.billing_type for component in components)

        if billing_types == {BillingTypes.LIMIT}:
            return "limit_only"
        elif billing_types == {BillingTypes.USAGE}:
            return "usage_only"
        else:
            return "mixed"

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_effective_available_limits(self, offering: models.Offering) -> list[str]:
        """
        Returns the union of plugin-registered available limits and
        custom LIMIT-type components added by the service provider.
        """
        plugin_limits = plugins.manager.get_available_limits(offering.type)
        builtin_types = {c.type for c in plugins.manager.get_components(offering.type)}
        custom_limit_types = list(
            offering.components.filter(billing_type=BillingTypes.LIMIT)
            .exclude(type__in=builtin_types)
            .values_list("type", flat=True)
        )
        return list(set(plugin_limits + custom_limit_types))


set_override(
    ProviderOfferingDetailsSerializer,
    "optional_fields",
    ["secret_options", "service_attributes"],
)


class PublicOfferingDetailsSerializer(ProviderOfferingDetailsSerializer):
    class Meta(ProviderOfferingDetailsSerializer.Meta):
        view_name = "marketplace-public-offering-detail"
        fields = tuple(
            f
            for f in ProviderOfferingDetailsSerializer.Meta.fields
            if f != "backend_id_rules"
        ) + (
            "user_has_consent",
            "is_accessible",
        )

    plugin_options = MergedPluginOptionsField(read_only=True)
    user_has_consent = serializers.SerializerMethodField()
    is_accessible = serializers.SerializerMethodField()

    @extend_schema_field(BasePublicPlanSerializer(many=True))
    def get_filtered_plans(self, offering: models.Offering):
        customer_uuid = self.context["request"].GET.get("allowed_customer_uuid")
        user = self.context["request"].user
        qs = utils.get_plans_available_for_user(
            user=user, offering=offering, allowed_customer_uuid=customer_uuid
        )
        return BasePublicPlanSerializer(qs, many=True, context=self.context).data

    @extend_schema_field(serializers.BooleanField())
    def get_user_has_consent(self, offering: models.Offering) -> bool:
        """Check if the current user has active consent for this offering."""
        request = self.context.get("request")
        if not request or not request.user or request.user.is_anonymous:
            return False

        return models.UserOfferingConsent.objects.filter(
            user=request.user, offering=offering, revocation_date__isnull=True
        ).exists()

    @extend_schema_field(serializers.BooleanField())
    def get_is_accessible(self, offering: models.Offering) -> bool:
        """Returns True if current user can order this offering."""
        request = self.context.get("request")
        if not request or not request.user or request.user.is_anonymous:
            return False
        if request.user.is_staff or request.user.is_support:
            return True
        # Check if user has accessible plans for this offering
        plans = utils.get_plans_available_for_user(request.user, offering)
        return plans.filter(archived=False).exists()

    def get_fields(self):
        fields = super().get_fields()
        fields.pop("secret_options", None)
        fields.pop("service_attributes", None)
        return fields


class OfferingComponentLimitSerializer(serializers.Serializer):
    min = serializers.IntegerField(min_value=0, help_text="Minimum allowed value")
    max = serializers.IntegerField(min_value=0, help_text="Maximum allowed value")
    max_available_limit = serializers.IntegerField(
        min_value=0, help_text="Maximum available limit across all resources"
    )


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
    plugin_options = MergedPluginOptionsSerializer(required=False)
    description = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    full_description = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    vendor_details = core_serializers.HTMLCleanField(required=False, allow_blank=True)

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
        # Validate against registered and enabled offering types
        if offering_type not in plugins.manager.get_offering_types():
            raise rf_exceptions.ValidationError(_("Invalid value."))
        return offering_type

    def _validate_attributes(self, attrs):
        category = attrs.get("category")
        if category is None and self.instance:
            category = self.instance.category

        attributes = attrs.get("attributes")
        if attributes is not None and not isinstance(attributes, dict):
            raise rf_exceptions.ValidationError(
                {"attributes": _("Dictionary is expected.")}
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
    description = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    full_description = core_serializers.HTMLCleanField(required=False, allow_blank=True)

    class Meta:
        model = models.Offering
        fields = (
            "name",
            "description",
            "full_description",
            "privacy_policy_link",
            "helpdesk_url",
            "documentation_url",
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


class OfferingComplianceChecklistUpdateSerializer(serializers.ModelSerializer):
    compliance_checklist = serializers.SlugRelatedField(
        queryset=checklist_models.Checklist.objects.filter(
            checklist_type=checklist_enums.ChecklistTypes.OFFERING_COMPLIANCE
        ),
        slug_field="uuid",
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.Offering
        fields = ("compliance_checklist",)


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

    def validate(self, attrs):
        user = self.context["request"].user
        if not user.is_staff:
            plugin_options = attrs.get("plugin_options", {})
            if "disabled_resource_actions" in plugin_options:
                # Check if it's actually changing
                old_value = self.instance.plugin_options.get(
                    "disabled_resource_actions", []
                )
                new_value = plugin_options["disabled_resource_actions"]
                if old_value != new_value:
                    raise rf_exceptions.ValidationError(
                        {
                            "plugin_options": _(
                                "Only staff can change list of disabled actions."
                            )
                        }
                    )
        return attrs

    def get_fields(self):
        fields = super().get_fields()
        for field in fields.values():
            if hasattr(field, "fields"):
                for subfield in field.fields.values():
                    subfield.default = serializers.empty
        return fields

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
            if isinstance(value, datetime.date | datetime.datetime):
                value = value.isoformat()
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
            "role": {
                "view_name": "role-detail",
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
            "offering_shared",
            "offering_billable",
            "offering_plugin_options",
            "provider_name",
            "provider_uuid",
            "provider_slug",
            "provider_description",
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
    provider_slug = serializers.ReadOnlyField(
        read_only=True, source="offering.customer.slug"
    )
    provider_description = serializers.ReadOnlyField(
        source="offering.customer.description"
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


def _validate_prepaid_duration_against_component(
    duration_in_months: int, component, field_name: str
):
    """
    Validate duration against a single component's min/max/step constraints.
    Raises serializers.ValidationError if invalid.
    """
    min_dur = component.min_prepaid_duration
    max_dur = component.max_prepaid_duration
    step = component.prepaid_duration_step or 1

    if min_dur is not None and duration_in_months < min_dur:
        raise serializers.ValidationError(
            {
                field_name: _(
                    "The selected duration of {d} months is less than the minimum "
                    "required duration of {min} months for component '{name}'."
                ).format(d=duration_in_months, min=min_dur, name=component.name)
            }
        )

    if max_dur is not None and duration_in_months > max_dur:
        raise serializers.ValidationError(
            {
                field_name: _(
                    "The selected duration of {d} months exceeds the maximum "
                    "allowed duration of {max} months for component '{name}'."
                ).format(d=duration_in_months, max=max_dur, name=component.name)
            }
        )

    if step > 1:
        base = min_dur or 0
        if (duration_in_months - base) % step != 0:
            raise serializers.ValidationError(
                {
                    field_name: _(
                        "The selected duration of {d} months is not valid for component '{name}'. "
                        "Valid durations start at {base} months with a step of {step} months "
                        "(e.g. {base}, {next}, ...)."
                    ).format(
                        d=duration_in_months,
                        name=component.name,
                        base=base,
                        step=step,
                        next=base + step,
                    )
                }
            )


class BaseOrderSerializer(BaseItemSerializer):
    class Meta(BaseItemSerializer.Meta):
        model = models.Order
        fields = BaseItemSerializer.Meta.fields + (
            "resource_uuid",
            "resource_type",
            "resource_name",
            "cost",
            "state",
            "output",
            "output_updated_at",
            "marketplace_resource_uuid",
            "error_message",
            "error_traceback",
            "error_updated_at",
            "accepting_terms_of_service",
            "callback_url",
            "completed_at",
            "request_comment",
            "attachment",
            "type",
            "start_date",
            "slug",
        )

        read_only_fields = (
            "cost",
            "state",
            "error_message",
            "error_traceback",
            "output",
            "output_updated_at",
            "error_updated_at",
            "completed_at",
            "slug",
        )
        protected_fields = (
            "offering",
            "plan",
            "callback_url",
            "request_comment",
            "attachment",
            "start_date",
        )

    type = NaturalChoiceField(
        choices=OrderTypes.CHOICES,
        required=False,
        default=OrderTypes.CREATE,
    )
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

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        request = self.context["view"].request
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        user = request.user
        # conceal detailed error message from non-system users
        if (
            not user.is_authenticated or (not user.is_staff and not user.is_support)
        ) and "error_traceback" in fields:
            del fields["error_traceback"]
        return fields


class OrderUpdateSerializer(BaseOrderSerializer):
    class Meta(BaseOrderSerializer.Meta):
        fields = ("limits", "attributes", "start_date")
        protected_fields = ()

    def validate_attributes(self, attributes):
        validate_attributes(attributes, self.instance.offering.category)
        return attributes

    def validate(self, attrs):
        limits = attrs.get("limits")
        if limits:
            validate_limits(limits, self.instance.offering, self.instance.resource)
        return attrs


class OrderApproveByProviderSerializer(serializers.Serializer):
    attributes = serializers.JSONField(required=False)

    def validate_attributes(self, attributes):
        if not attributes:
            return attributes
        order = self.context["view"].get_object()
        new_options = attributes.get("new_options")
        if new_options:
            resource_options = order.offering.resource_options
            if not resource_options or not resource_options.get("options"):
                raise serializers.ValidationError(
                    _("Metadata for resource options is not defined.")
                )
            validate_options(resource_options["options"], new_options, optional=True)
        return attributes


class OrderProviderInfoSerializer(serializers.Serializer):
    provider_message = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    provider_message_url = serializers.URLField(required=False, allow_blank=True)
    provider_message_attachment = serializers.FileField(required=False)


class OrderConsumerInfoSerializer(serializers.Serializer):
    consumer_message = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    consumer_message_attachment = serializers.FileField(required=False)


class OrderInfoResponseSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True)


class OrderDetailsSerializer(BaseOrderSerializer):
    class Meta(BaseOrderSerializer.Meta):
        fields = BaseOrderSerializer.Meta.fields + (
            "url",
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
            "created_by_email",
            "created_by_organization",
            "created_by_organization_registry_code",
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
            "order_subtype",
            "provider_message",
            "provider_message_url",
            "provider_message_attachment",
            "consumer_message",
            "consumer_message_attachment",
            "consumer_rejection_comment",
            "provider_rejection_comment",
        )
        extra_kwargs = {
            **BaseOrderSerializer.Meta.extra_kwargs,
            "url": {"lookup_field": "uuid", "view_name": "marketplace-order-detail"},
        }

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
    created_by_email = serializers.ReadOnlyField(
        source="created_by.email",
        allow_null=True,
    )
    created_by_organization = serializers.ReadOnlyField(
        source="created_by.organization",
        allow_null=True,
    )
    created_by_organization_registry_code = serializers.ReadOnlyField(
        source="created_by.organization_registry_code",
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

    new_cost_estimate = serializers.ReadOnlyField(
        source="cost",
        allow_null=True,
    )

    order_subtype = serializers.SerializerMethodField()

    def get_order_subtype(self, order) -> str | None:
        if order.type != OrderTypes.UPDATE:
            return None
        if order.attributes.get("action") == "renew":
            return "renew"
        if "old_limits" in order.attributes:
            return "update_limits"
        if "new_options" in order.attributes:
            return "update_options"
        return "plan_switch"

    can_terminate = serializers.SerializerMethodField()
    termination_comment = serializers.ReadOnlyField()
    consumer_rejection_comment = serializers.ReadOnlyField()
    provider_rejection_comment = serializers.ReadOnlyField()

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


set_override(OrderDetailsSerializer, "optional_fields", ["error_traceback"])


class OrderErrorDetailsSerializer(
    serializers.ModelSerializer, core_serializers.AugmentedSerializerMixin
):
    class Meta:
        model = models.Order
        fields = ("error_message", "error_traceback", "consumer_rejection_comment")
        protected_fields = (
            "error_message",
            "error_traceback",
            "consumer_rejection_comment",
        )


class OrderProviderRejectionSerializer(
    serializers.ModelSerializer, core_serializers.AugmentedSerializerMixin
):
    class Meta:
        model = models.Order
        fields = ("provider_rejection_comment",)
        protected_fields = ("provider_rejection_comment",)


def validate_public_offering(order: models.Order, request):
    """Validate that the customer is allowed to order a public offering."""
    # Staff users can override access policy restrictions
    if request.user.is_staff:
        return

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

    raise serializers.ValidationError(
        _(
            "This offering is not available for ordering due to the org group limitation."
        )
    )


def validate_private_offering(order: models.Order, request):
    """Validate that the customer is allowed to order a private offering."""
    # Staff users can override access policy restrictions
    if request.user.is_staff:
        return

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


def confirm_order_request_user_has_offering_consent(
    order: models.Order, request
) -> None:
    """Check that the user has accepted the offering's Terms of Service for an order request."""
    if request.user.is_staff or request.user.is_support:
        return

    if not order.offering.plugin_options.get(
        "service_provider_can_create_offering_user", False
    ):
        return

    if not order.offering.has_terms_of_service():
        return

    if order.offering.check_user_consent(request.user):
        return

    accepting_terms = request.data.get("accepting_terms_of_service", False)
    if not accepting_terms:
        raise serializers.ValidationError(
            _("You must accept Terms of Service before creating orders.")
        )

    active_tos = order.offering.terms_of_service_configs.filter(is_active=True).first()
    version = active_tos.version if active_tos else ""
    models.UserOfferingConsent.objects.get_or_create(
        user=request.user,
        offering=order.offering,
        defaults={"version": version},
    )


def validate_order(order: models.Order, request):
    """Validate order creation."""
    structure_utils.check_customer_blocked_or_archived(order.project.customer)

    if order.offering.state == OfferingStates.UNAVAILABLE:
        raise serializers.ValidationError(_("Offering is not available."))

    if order.type != OrderTypes.TERMINATE:
        structure_utils.check_project_end_date(order.project)

        if order.offering.state not in (
            OfferingStates.ACTIVE,
            OfferingStates.PAUSED,
        ):
            raise serializers.ValidationError(_("Offering is not available."))

    if order.offering.shared:
        validate_public_offering(order, request)
    else:
        validate_private_offering(order, request)

    if check_pending_order_exists(order.resource):
        raise serializers.ValidationError(
            _("Pending order for resource already exists.")
        )
    if config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
        confirm_order_request_user_has_offering_consent(order, request)

    utils.validate_order(order, request)


class OrderCreateSerializer(
    BaseOrderSerializer,
    core_serializers.SlugSerializerMixin,
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
        read_only_fields = BaseOrderSerializer.Meta.read_only_fields + (
            "created_by",
            "consumer_reviewed_by",
            "consumer_reviewed_at",
            "attachment",
        )
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

    def get_fields(self):
        fields = super().get_fields()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if not config.ENABLE_ORDER_START_DATE:
            fields.pop("start_date", None)
        return fields

    def generate_slug(self, validated_data):
        return models.Order(
            project=validated_data["project"], offering=validated_data["offering"]
        ).generate_slug()

    def validate_project(
        self, project: structure_models.Project
    ) -> structure_models.Project:
        """Validate that the project is not soft-deleted."""
        if project.is_removed:
            raise serializers.ValidationError(
                _("Cannot create orders for terminated projects.")
            )
        return project

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        project: structure_models.Project = validated_data["project"]
        attributes = validated_data.get("attributes", {})
        resource = models.Resource(
            project=project,
            offering=validated_data["offering"],
            plan=validated_data.get("plan"),
            limits=validated_data.get("limits") or {},
            attributes=attributes,
            name=attributes.get("name") or "",
        )
        resource.init_cost()
        end_date = validate_end_date(
            resource.offering,
            resource.created.date(),
            parse_date(attributes.get("end_date")),
        )
        if end_date:
            resource.end_date = end_date
            resource.end_date_requested_by = (
                request.user if attributes.get("end_date") else None
            )

        # Set resource options from offering's resource_options
        resource.options = {}
        for resource_option in (
            validated_data["offering"].resource_options.get("options", {}).keys()
        ):
            if resource_option in attributes:
                resource.options[resource_option] = attributes[resource_option]

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
            start_date=validated_data.get("start_date"),
        )
        validate_order(order, request)
        self.quotas_validate(order)
        order.init_cost()
        order.save()
        return order

    def get_filtered_field_names(self):
        return ("project",)

    def quotas_validate(self, order: models.Order):
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
        """
        Main validation coordinator. Extracts context and calls specific validators.
        """
        attrs = super().validate(attrs)

        # Extract Context
        request = self.context["request"]
        user: User = request.user
        offering: models.Offering = attrs["offering"]
        project: structure_models.Project = attrs["project"]
        attributes = attrs.get("attributes", {})
        accepting_tos = attrs.get("accepting_terms_of_service", False)

        # Execute Validation Blocks
        self._validate_resource_name(attributes)
        self._validate_terms_of_service(user, offering, accepting_tos)
        self._validate_project_not_terminated(project)
        self._validate_project_policy_constraints(project, offering, attributes)
        self._validate_plan_for_create(attrs, offering)

        # Prepaid Offering Validation
        prepaid_components = offering.components.filter(is_prepaid=True)
        if prepaid_components.exists():
            self._validate_prepaid_attributes(attributes, prepaid_components)

        self._validate_order_start_date(attrs)

        return attrs

    def _validate_resource_name(self, attributes):
        name = attributes.get("name") or ""
        if len(name) > NAME_LENGTH:
            raise ValidationError(
                {
                    "attributes.name": _(
                        "Name is too long. Maximum number of symbols is %s"
                    )
                    % NAME_LENGTH
                }
            )

    def _validate_terms_of_service(self, user, offering, accepting_tos):
        """
        Checks if ToS are required and if the user has accepted them.
        """
        if not config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
            return

        if not offering.has_terms_of_service():
            return

        # Staff and support are exempt
        if user.is_staff or user.is_support:
            return

        # Check if accepted in this request or previously
        if not accepting_tos and not offering.check_user_consent(user):
            raise ValidationError(
                _("Terms of service for offering '%s' have not been accepted.")
                % offering
            )

    def _validate_project_not_terminated(self, project):
        """
        Validates that the project is not soft-deleted/terminated.
        Prevents creating new marketplace orders for terminated projects.
        """
        if project.is_removed:
            raise ValidationError(_("Cannot create orders for terminated projects."))

    def _validate_project_policy_constraints(self, project, offering, attributes=None):
        """
        Validates offering-defined constraints on the target project.
        """
        self._validate_minimal_team_count(project, offering)
        self._validate_required_team_role(project, offering)
        self._validate_maximal_resource_count(project, offering)
        if attributes is not None:
            self._validate_unique_resource_per_attribute(project, offering, attributes)

    def _validate_minimal_team_count(self, project, offering):
        min_count_setting = offering.plugin_options.get(
            "minimal_team_count_for_provisioning"
        )
        if not min_count_setting:
            return

        try:
            min_count = int(min_count_setting)
        except (ValueError, TypeError):
            raise ValidationError(
                "Invalid configuration: minimal_team_count_for_provisioning must be an integer."
            )

        project_user_count = project.get_users().count()
        if project_user_count < min_count:
            raise ValidationError(
                _(
                    "Project '%(project)s' does not meet the minimal team size "
                    "required by this offering. Required: %(required)s, Actual: %(actual)s."
                )
                % {
                    "project": project.name,
                    "required": min_count,
                    "actual": project_user_count,
                }
            )

    def _validate_required_team_role(self, project, offering):
        required_role_name = offering.plugin_options.get(
            "required_team_role_for_provisioning"
        )
        if not required_role_name:
            return

        project_ct = ContentType.objects.get_for_model(structure_models.Project)
        role = permission_models.Role.objects.filter(
            name=required_role_name,
            content_type=project_ct,
            is_active=True,
        ).first()

        if not role:
            raise ValidationError(
                _("Configuration Error: The required project role '%s' does not exist.")
                % required_role_name
            )

        if not project.get_users(role).exists():
            raise ValidationError(
                _(
                    "Project '%(project)s' must have at least one user with the role '%(role)s' "
                    "to provision this offering."
                )
                % {"project": project.name, "role": required_role_name}
            )

    def _validate_maximal_resource_count(self, project, offering):
        max_count_setting = offering.plugin_options.get(
            "maximal_resource_count_per_project"
        )
        if max_count_setting is None:
            return

        try:
            limit = int(max_count_setting)
        except (ValueError, TypeError):
            raise ValidationError(
                "Invalid configuration: maximal_resource_count_per_project must be an integer."
            )

        # Count non-terminated resources for this project and offering
        current_count = (
            models.Resource.objects.filter(project=project, offering=offering)
            .exclude(state=models.Resource.States.TERMINATED)
            .count()
        )

        if current_count >= limit:
            raise ValidationError(
                _(
                    "Project '%(project)s' has reached the maximum number of resources (%(limit)s) "
                    "allowed for this offering."
                )
                % {"project": project.name, "limit": limit}
            )

    def _validate_unique_resource_per_attribute(self, project, offering, attributes):
        """
        Validates that only one non-terminated resource per unique attribute value
        exists for a project+offering combination.

        Configuration via offering.plugin_options:
            "unique_resource_per_attribute": "attribute_name"

        Example: With unique_resource_per_attribute="storage_data_type",
        a project can have one "Store" and one "Archive" resource,
        but cannot have two "Store" resources.
        """
        unique_attr = offering.plugin_options.get("unique_resource_per_attribute")
        if not unique_attr:
            return

        new_attr_value = attributes.get(unique_attr)
        if not new_attr_value:
            # Attribute not provided in order - skip validation
            # (attribute validation should catch required fields separately)
            return

        existing = (
            models.Resource.objects.filter(
                project=project,
                offering=offering,
            )
            .exclude(state=models.Resource.States.TERMINATED)
            .filter(attributes__contains={unique_attr: new_attr_value})
            .exists()
        )

        if existing:
            raise ValidationError(
                _(
                    "Project '%(project)s' already has a resource with %(attr)s='%(value)s' "
                    "for this offering. Only one resource per %(attr)s value is allowed."
                )
                % {
                    "project": project.name,
                    "attr": unique_attr,
                    "value": new_attr_value,
                }
            )

    def _validate_prepaid_attributes(
        self, attributes, prepaid_components: QuerySet[models.OfferingComponent]
    ):
        """
        Validates attributes specific to prepaid offerings (end_date, duration).
        """

        # Rule 1: 'end_date' is mandatory for prepaid offerings.
        end_date_str = attributes.get("end_date")
        if not end_date_str:
            raise ValidationError(
                {
                    "attributes.end_date": _(
                        "This field is required for prepaid offerings."
                    )
                }
            )

        try:
            end_date = datetime.date.fromisoformat(end_date_str)
        except (ValueError, TypeError):
            raise ValidationError(
                {"attributes.end_date": _("Invalid date format. Use YYYY-MM-DD.")}
            )

        if end_date <= timezone.now().date():
            raise ValidationError(
                {"attributes.end_date": _("End date must be in the future.")}
            )

        # Rule 2: Validate duration against component constraints.
        start_date = timezone.now().date()
        # Calculate duration in full months. A partial month at the end counts as a full month.
        delta = relativedelta(end_date, start_date)
        duration_in_months = delta.years * 12 + delta.months
        if delta.days > 0:
            duration_in_months += 1

        # Check against every prepaid component's duration limits
        for component in prepaid_components:
            _validate_prepaid_duration_against_component(
                duration_in_months, component, "attributes.end_date"
            )

    def _validate_plan_for_create(self, attrs, offering):
        """
        Plan is required for CREATE orders.
        Private offerings can be created without plans.
        """
        order_type = attrs.get("type", OrderTypes.CREATE)
        plan = attrs.get("plan")

        if order_type != OrderTypes.CREATE:
            return

        # Private offerings (shared=False) can be created without plans
        if not offering.shared:
            return

        if not plan:
            raise ValidationError(
                {"plan": _("Plan is required when creating resources.")}
            )

    def _validate_order_start_date(self, attrs):
        start_date = attrs.get("start_date")
        if not start_date:
            return

        # Basic validation: must not be in the past
        if start_date < timezone.now().date():
            raise serializers.ValidationError(
                {"start_date": _("Start date cannot be in the past.")}
            )

        project: structure_models.Project = attrs["project"]

        # Validate against project's lifecycle
        if project.start_date and start_date < project.start_date:
            raise serializers.ValidationError(
                {
                    "start_date": _(
                        "Order start date cannot be earlier than the project start date (%(project_start_date)s)."
                    )
                    % {"project_start_date": project.start_date}
                }
            )

        if project.end_date and start_date > project.end_date:
            raise serializers.ValidationError(
                {
                    "start_date": _(
                        "Order start date cannot be later than the project end date (%(project_end_date)s)."
                    )
                    % {"project_end_date": project.end_date}
                }
            )


set_override(
    OrderCreateSerializer, "optional_fields", ["start_date", "error_traceback"]
)


class OrderAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Order
        fields = ("attachment",)


class BackendMetadataSerializer(serializers.Serializer):
    state = serializers.CharField(read_only=True, help_text="Backend resource state")
    runtime_state = serializers.CharField(
        read_only=True, help_text="Runtime state of the backend resource"
    )
    action = serializers.CharField(
        read_only=True, help_text="Current action being performed"
    )
    instance_name = serializers.CharField(
        read_only=True, allow_null=True, help_text="Name of the backend instance"
    )


class ResourceSuggestNameSerializer(serializers.ModelSerializer):
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.objects.all(), slug_field="uuid"
    )
    offering = serializers.SlugRelatedField(
        queryset=models.Offering.objects.all(), slug_field="uuid"
    )
    plan = serializers.SlugRelatedField(
        queryset=models.Plan.objects.all(),
        slug_field="uuid",
        required=False,
        allow_null=True,
    )
    attributes = serializers.JSONField(required=False, default=dict)

    class Meta:
        model = models.Resource
        fields = ("project", "offering", "plan", "attributes")

    def get_fields(self):
        fields = super().get_fields()

        request = self.context["request"]

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        user = request.user
        fields["project"].queryset = filter_queryset_for_user(
            fields["project"].queryset, user
        )
        fields["offering"].queryset = fields[
            "offering"
        ].queryset.filter_by_ordering_availability_for_user(user)
        return fields


class ResourceSerializer(core_serializers.SlugSerializerMixin, BaseItemSerializer):
    project_slug = serializers.ReadOnlyField(source="project.slug")
    customer_slug = serializers.ReadOnlyField(source="project.customer.slug")
    renewal_date = serializers.SerializerMethodField()
    offering_state = serializers.SerializerMethodField()
    offering_components = serializers.SerializerMethodField()

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
            "project_effective_end_date",
            "project_end_date_requested_by",
            "customer_uuid",
            "customer_name",
            "offering_uuid",
            "offering_name",
            "offering_slug",
            "parent_offering_uuid",
            "parent_offering_name",
            "parent_offering_slug",
            "offering_backend_id",
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
            "options",
            "available_actions",
            "last_sync",
            "order_in_progress",
            "creation_order",
            "service_settings_uuid",
            "project_slug",
            "customer_slug",
            "user_requires_reconsent",
            "renewal_date",
            "offering_state",
            "offering_components",
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
            "project_slug",
            "customer_slug",
            "offering_components",
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
    service_settings_uuid = serializers.UUIDField(
        read_only=True, source="scope.service_settings.uuid"
    )
    project = serializers.HyperlinkedRelatedField(
        lookup_field="uuid",
        view_name="project-detail",
        read_only=True,
    )
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.ReadOnlyField(source="project.name")
    project_end_date = serializers.ReadOnlyField(source="project.end_date")
    project_effective_end_date = serializers.DateField(
        read_only=True,
        allow_null=True,
        source="project.end_date_with_grace",
        help_text="Effective project end date including grace period. After this date, resources will be terminated.",
    )
    project_end_date_requested_by = serializers.HyperlinkedRelatedField(
        source="project.end_date_requested_by",
        lookup_field="uuid",
        view_name="user-detail",
        read_only=True,
        allow_null=True,
    )
    project_description = serializers.ReadOnlyField(source="project.description")
    customer_name = serializers.ReadOnlyField(source="project.customer.name")
    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    offering_slug = serializers.ReadOnlyField(source="offering.slug")
    offering_backend_id = serializers.ReadOnlyField(source="offering.backend_id")
    parent_offering_uuid = serializers.UUIDField(
        read_only=True, source="offering.parent.uuid"
    )
    parent_offering_name = serializers.ReadOnlyField(source="offering.parent.name")
    parent_uuid = serializers.UUIDField(read_only=True, source="parent.uuid")
    parent_name = serializers.ReadOnlyField(source="parent.name")
    parent_offering_slug = serializers.ReadOnlyField(source="parent.slug")
    # If resource is usage-based, frontend would render button to show and report usage
    is_usage_based = serializers.ReadOnlyField(
        source="offering.is_usage_based",
        help_text="Returns True if the resource has usage-based components that track variable consumption.",
    )
    is_limit_based = serializers.ReadOnlyField(
        source="offering.is_limit_based",
        help_text="Returns True if the resource has limit-based components with user-adjustable quotas.",
    )
    can_terminate = serializers.SerializerMethodField()
    report = ResourceReportField(read_only=True)
    username = serializers.SerializerMethodField()
    limit_usage = serializers.SerializerMethodField(
        help_text="Dictionary mapping limit-based component types to their consumed usage. "
        "For monthly periods, maps from current_usages; for longer periods, aggregates historical usage."
    )
    endpoints = NestedEndpointSerializer(many=True, read_only=True)
    available_actions = serializers.SerializerMethodField()
    limits = serializers.SerializerMethodField()
    attributes = serializers.SerializerMethodField()
    current_usages = serializers.SerializerMethodField(
        help_text="Dictionary mapping component types to their latest reported usage amounts."
    )
    order_in_progress = serializers.SerializerMethodField(allow_null=True)
    creation_order = serializers.SerializerMethodField(allow_null=True)
    backend_metadata = serializers.SerializerMethodField()
    user_requires_reconsent = serializers.SerializerMethodField()

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

    @extend_schema_field(
        serializers.DictField(
            child=serializers.FloatField(),
            help_text="Dictionary mapping limit-based component types to their consumed usage. "
            "For monthly periods, maps from current_usages; for longer periods, aggregates historical usage.",
        )
    )
    def get_limit_usage(self, resource: models.Resource) -> dict[str, float]:
        """
        Calculates and returns the consumption of limit-based components.
        For components with a monthly (or unspecified) limit period, it attempts to
        fetch the value directly from the resource's `current_usages` JSONField.
        For other periods (like annual), it calculates the sum from `ComponentUsage` tracking data.
        """
        if not resource.offering.is_limit_based or not resource.plan:
            return {}

        return utils.get_current_period_usage(resource)

    def get_available_actions(self, resource: models.Resource) -> list[str]:
        return plugins.manager.get_available_resource_actions(resource)

    def get_limits(self, resource: models.Resource) -> dict[str, int]:
        return resource.limits

    def get_attributes(self, resource: models.Resource) -> dict:
        return resource.safe_attributes

    @extend_schema_field(
        serializers.DictField(
            child=serializers.FloatField(),
            help_text="Dictionary mapping component types to their latest reported usage amounts.",
        )
    )
    def get_current_usages(self, resource: models.Resource) -> dict[str, float]:
        return resource.current_usages

    @extend_schema_field(BackendMetadataSerializer)
    def get_backend_metadata(self, resource: models.Resource):
        return resource.backend_metadata

    def get_user_requires_reconsent(self, resource: models.Resource) -> bool:
        """Check if the current user needs to re-consent for this resource's offering."""
        if not config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
            return False

        request = self.context.get("request")
        if not request or not request.user or request.user.is_anonymous:
            return False

        user = request.user
        offering = resource.offering

        if (
            user.is_staff
            or user.is_support
            or not offering.plugin_options.get(
                "service_provider_can_create_offering_user", False
            )
            or not offering.has_terms_of_service()
        ):
            return False

        consent = models.UserOfferingConsent.objects.filter(
            user=user,
            offering=offering,
            revocation_date__isnull=True,
        ).first()

        if not consent:
            return True

        # Check if active ToS requires reconsent AND user's version is outdated
        active_tos = offering.terms_of_service_configs.filter(is_active=True).first()

        if not active_tos or not active_tos.requires_reconsent:
            return False

        return consent.version != active_tos.version

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

    @extend_schema_field(serializers.ChoiceField(choices=OfferingStates.VALUES))
    def get_offering_state(self, resource: models.Resource):
        return resource.offering.get_state_display()

    @extend_schema_field(OfferingComponentSerializer(many=True))
    def get_offering_components(self, resource: models.Resource):
        """
        Get offering components with their billing type and limit period.

        Returns:
            list: List of offering components for the resource's offering
        """
        if not resource.offering_id:
            return []

        components = resource.offering.components.all()
        return OfferingComponentSerializer(
            components, many=True, context=self.context
        ).data

    def get_fields(self):
        fields = super().get_fields()
        if "attributes" in fields:
            fields["attributes"] = serializers.SerializerMethodField()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        request = self.context["request"]
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        keys = request.query_params.getlist(self.FIELDS_PARAM_NAME)
        for key in ("order_in_progress", "creation_order"):
            if keys and key not in keys and key in fields:
                del fields[key]
        return fields

    @extend_schema_field(
        serializers.DictField(child=serializers.DateField(), allow_null=True)
    )
    def get_renewal_date(self, resource: models.Resource):
        """
        Calculate renewal dates for all limit-based components in the resource's offering.

        Returns a dictionary mapping component types to their next renewal dates.
        The renewal date is the first day of the month when the next invoice item
        for each component would be created by the billing service.

        Returns:
            dict: Mapping of component_type -> renewal_date for limit-based components
            None: If no limit-based components exist
        """
        # Check if resource has an offering with limit-based components
        if not resource.offering_id:
            return None

        # Use values_list to get only the data we need in a single query
        # This avoids N+1 queries by fetching type and limit_period in bulk
        limit_components_data = models.OfferingComponent.objects.filter(
            offering_id=resource.offering_id,
            billing_type=BillingTypes.LIMIT,
            limit_period__isnull=False,
        ).values_list("type", "limit_period")

        if not limit_components_data:
            return None

        # Calculate renewal date for each component using the fetched data
        renewal_dates = {}

        for component_type, limit_period in limit_components_data:
            renewal_date = self._calculate_renewal_date_for_period(limit_period)
            if renewal_date:
                renewal_dates[component_type] = renewal_date

        return renewal_dates

    def _calculate_renewal_date_for_period(self, limit_period):
        """
        Calculate the next renewal date for a specific limit period.

        Args:
            limit_period: The billing period (MONTH, QUARTERLY, ANNUAL, TOTAL)

        Returns:
            date: Next renewal date, or None for TOTAL periods
        """
        now = timezone.now().date()
        if limit_period == LimitPeriods.MONTH:
            # Monthly billing: next month's first day
            return (now + relativedelta(months=1)).replace(day=1)

        elif limit_period == LimitPeriods.QUARTERLY:
            # Quarterly billing: first day of next quarter
            # Quarters start in January, April, July, October
            current_quarter = ((now.month - 1) // 3) + 1
            if current_quarter == 4:
                # Q4 -> Q1 next year
                return datetime.date(now.year + 1, 1, 1)
            else:
                # Move to next quarter
                next_quarter_month = current_quarter * 3 + 1
                return datetime.date(now.year, next_quarter_month, 1)

        elif limit_period == LimitPeriods.ANNUAL:
            # Annual billing: first day of next year
            return datetime.date(now.year + 1, 1, 1)

        elif limit_period == LimitPeriods.TOTAL:
            # No renewal for total limits
            return None
        else:
            return None


class OrderUUIDSerializer(serializers.Serializer):
    order_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the created or updated order"
    )


class ResourceReallocateLimitsResponseSerializer(serializers.Serializer):
    source_order_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the source order for limit reallocation"
    )
    target_order_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        read_only=True,
        help_text="List of UUIDs for target orders receiving the reallocated limits",
    )


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
        plan: models.Plan = attrs["plan"]
        resource: models.Resource = self.context["view"].get_object()

        if plan.offering != resource.offering:
            raise rf_exceptions.ValidationError(
                {"plan": _("Plan is not available for this offering.")}
            )

        validate_plan(plan)

        if plan.unit != resource.plan.unit:
            raise rf_exceptions.ValidationError(
                {"plan": _("Billing period of new plan must match the old one.")}
            )

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
        """
        Comprehensive validation for the end_date field.

        This method layers validation checks:
        1. It first enforces the strict rule that prepaid resources cannot have their
           end_date modified directly via this serializer.
        2. If that passes, it calls the generic utility function to validate against
           offering-specific rules defined in plugin_options.
        """
        # We need the resource instance to perform validation.
        # If it's not available (e.g., on create, though this is an update serializer),
        # we can't perform these checks.
        if not self.instance:
            return end_date

        if not end_date:
            return end_date  # Allowing end_date to be cleared is valid

        if end_date < timezone.datetime.today().date():
            raise serializers.ValidationError(
                _("End date cannot be earlier than the current date.")
            )

        resource: models.Resource = self.instance

        if resource and resource.offering.components.filter(is_prepaid=True).exists():
            # Check if the end_date is actually being changed.
            # No error if the user submits the same end_date.
            if resource.end_date != end_date:
                raise serializers.ValidationError(
                    _(
                        "Direct modification of the end date is not allowed for prepaid resources. "
                        "Please use the 'renew' action to extend the subscription."
                    )
                )

        # The utility function handles all other cases (max offset, required date, etc.)
        end_date = validate_end_date(
            offering=resource.offering,
            created_date=resource.created.date(),
            end_date=end_date,
        )
        return end_date

    def save(self, **kwargs):
        """
        Custom save method to handle setting the 'end_date_requested_by' field
        and logging correctly.

        This method relies on the `validate_end_date` to have already performed
        all necessary checks. It only handles the database update.
        """
        resource = cast(models.Resource, self.instance)
        user = self.context["request"].user

        # Get values from validated_data, which has passed all checks.
        new_name = self.validated_data.get("name", resource.name)
        new_description = self.validated_data.get("description", resource.description)
        new_end_date = self.validated_data.get("end_date")

        updated_fields = []
        if resource.name != new_name:
            resource.name = new_name
            updated_fields.append("name")

        if resource.description != new_description:
            resource.description = new_description
            updated_fields.append("description")

        if resource.end_date != new_end_date:
            resource.end_date = new_end_date
            resource.end_date_requested_by = user
            updated_fields.extend(["end_date", "end_date_requested_by"])
            # Log the event only if the date actually changed.
            log.log_resource_end_date_has_been_updated(resource, user)

        if updated_fields:
            resource.save(update_fields=updated_fields)

        return resource


def _validate_renewal_duration_against_component(
    duration_in_months: int, component, field_name: str
):
    """
    Validate renewal duration against a component's renewal-specific
    min/max/step constraints. Raises serializers.ValidationError if invalid.
    """
    min_dur = component.min_renewal_duration
    max_dur = component.max_renewal_duration
    step = component.renewal_duration_step or 1

    if min_dur is not None and duration_in_months < min_dur:
        raise serializers.ValidationError(
            {
                field_name: _(
                    "The renewal duration of {d} months is less than the minimum "
                    "allowed renewal duration of {min} months for component '{name}'."
                ).format(d=duration_in_months, min=min_dur, name=component.name)
            }
        )

    if max_dur is not None and duration_in_months > max_dur:
        raise serializers.ValidationError(
            {
                field_name: _(
                    "The renewal duration of {d} months exceeds the maximum "
                    "allowed renewal duration of {max} months for component '{name}'."
                ).format(d=duration_in_months, max=max_dur, name=component.name)
            }
        )

    if step > 1:
        base = min_dur or 0
        if (duration_in_months - base) % step != 0:
            raise serializers.ValidationError(
                {
                    field_name: _(
                        "The renewal duration of {d} months is not valid for component '{name}'. "
                        "Valid durations start at {base} months with a step of {step} months "
                        "(e.g. {base}, {next}, ...)."
                    ).format(
                        d=duration_in_months,
                        name=component.name,
                        base=base,
                        step=step,
                        next=base + step,
                    )
                }
            )


MAX_RENEWAL_MONTHS = (
    600  # 50-year hard cap; per-component max_renewal_duration governs normal cases
)


class ResourceRenewSerializer(serializers.Serializer):
    """
    Serializer for validating the payload of a prepaid resource renewal action.
    """

    extension_months = serializers.IntegerField(
        min_value=1,
        max_value=MAX_RENEWAL_MONTHS,
        help_text=_("Number of months to extend the subscription by."),
    )
    limits = serializers.DictField(
        child=serializers.IntegerField(min_value=0),
        required=False,
        help_text=_("Optional new limits for the resource. Supports upgrades only."),
    )
    request_comment = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        help_text=_("Optional comment for the renewal request."),
    )
    attachment = serializers.FileField(
        required=False,
        help_text=_("Optional PDF attachment for the renewal request."),
    )

    def validate(self, attrs):
        """
        Ensure the resource is a renewable prepaid resource and validate renewal
        duration constraints from the offering component.
        """
        resource: models.Resource = self.context["resource"]
        prepaid_components = resource.offering.components.filter(is_prepaid=True)
        if not prepaid_components.exists():
            raise serializers.ValidationError(
                _("This action is only available for prepaid resources.")
            )
        extension_months = attrs.get("extension_months")
        if extension_months is not None:
            for component in prepaid_components:
                _validate_renewal_duration_against_component(
                    extension_months, component, "extension_months"
                )
        return attrs


class RenewalEstimateRequestSerializer(serializers.Serializer):
    extension_months = serializers.IntegerField(
        min_value=1, max_value=MAX_RENEWAL_MONTHS
    )
    limits = serializers.DictField(
        child=serializers.IntegerField(min_value=0), required=False
    )

    def validate(self, attrs):
        resource = self.context.get("resource")
        if resource is not None:
            extension_months = attrs.get("extension_months")
            if extension_months is not None:
                for component in resource.offering.components.filter(is_prepaid=True):
                    _validate_renewal_duration_against_component(
                        extension_months, component, "extension_months"
                    )
        return attrs


class RenewalEstimateComponentSerializer(serializers.Serializer):
    component_type = serializers.CharField()
    component_name = serializers.CharField()
    billing_type = serializers.CharField()
    billing_period = serializers.CharField(allow_null=True)
    current_limit = serializers.IntegerField()
    new_limit = serializers.IntegerField()
    unit_price = serializers.DecimalField(max_digits=22, decimal_places=10)
    measured_unit = serializers.CharField(allow_blank=True)
    period_description = serializers.CharField()
    total = serializers.DecimalField(max_digits=22, decimal_places=10)


class RenewalEstimateResponseSerializer(serializers.Serializer):
    components = RenewalEstimateComponentSerializer(many=True)
    subscription_total = serializers.DecimalField(max_digits=22, decimal_places=10)
    limit_change_total = serializers.DecimalField(max_digits=22, decimal_places=10)
    total = serializers.DecimalField(max_digits=22, decimal_places=10)
    remaining_days = serializers.IntegerField()
    new_end_date = serializers.DateField()


class ResourceEndDateByProviderSerializer(serializers.ModelSerializer):
    """Deprecated: Use ResourceEndDateSerializer instead."""

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


class ResourceEndDateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("end_date",)

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


class ResourceResponseStatusSerializer(serializers.Serializer):
    status = serializers.CharField(
        read_only=True, help_text="Status of the resource response"
    )


class ResourceUpdateLimitsSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Order
        fields = ("limits", "request_comment")

    limits = serializers.DictField(
        child=serializers.IntegerField(min_value=0), required=True
    )


class ResourceReallocateTargetSerializer(serializers.Serializer):
    resource_uuid = serializers.UUIDField(required=True)
    allocated_limits = serializers.DictField(
        child=serializers.IntegerField(min_value=1),
        required=True,
    )


class ResourceReallocateLimitsSerializer(serializers.Serializer):
    limits = serializers.DictField(
        child=serializers.IntegerField(min_value=1),
        required=True,
    )

    targets = ResourceReallocateTargetSerializer(many=True, required=True)


class ResourceBackendIDSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("backend_id",)


class ResourceEffectiveIDSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("effective_id",)


class OrderBackendIDSerializer(serializers.ModelSerializer):
    backend_id = serializers.CharField(write_only=True, required=True, max_length=255)

    class Meta:
        model = models.Order
        fields = ("backend_id",)


class OfferingBackendIdRulesUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Offering
        fields = ("backend_id_rules",)

    def validate_backend_id_rules(self, value):
        utils.validate_backend_id_rules(value)
        return value


class CheckUniqueBackendIDSerializer(serializers.Serializer):
    backend_id = serializers.CharField(
        required=True, max_length=255, help_text="Backend identifier to check"
    )
    check_all_offerings = serializers.BooleanField(
        required=False, default=False, help_text="Check across all offerings"
    )
    use_offering_rules = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Apply the offering's backend_id_rules for format and uniqueness validation",
    )


class CheckUniqueBackendIDResponseSerializer(serializers.Serializer):
    is_unique = serializers.BooleanField(help_text="Whether the backend ID is unique")
    is_valid_format = serializers.BooleanField(
        required=False,
        allow_null=True,
        help_text="Whether the backend ID matches the offering's format regex (null if no rules configured)",
    )
    errors = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of validation error messages",
    )


class ResourceSlugSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("slug",)


class ResourceDownscaledSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("downscaled",)


class ResourcePausedSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("paused",)


class ResourceRestrictMemberAccessSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("restrict_member_access",)


class ResourceVersionUserSerializer(serializers.Serializer):
    """Serializer for user information in resource version history."""

    uuid = serializers.UUIDField()
    username = serializers.CharField()
    full_name = serializers.CharField()


class ResourceVersionSerializer(serializers.Serializer):
    """Serializer for reversion Version objects for Resource history."""

    id = serializers.IntegerField()
    revision_date = serializers.DateTimeField(source="revision.date_created")
    revision_user = serializers.SerializerMethodField()
    revision_comment = serializers.CharField(
        source="revision.comment", allow_blank=True
    )
    serialized_data = serializers.SerializerMethodField()

    def get_revision_user(self, obj) -> dict | None:
        user = obj.revision.user
        if user:
            return {
                "uuid": user.uuid,
                "username": user.username,
                "full_name": user.full_name,
            }
        return None

    def get_serialized_data(self, obj) -> dict:
        import json

        return json.loads(obj.serialized_data)[0]["fields"]


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

        if (
            models.Order.objects.filter(
                resource=resource,
                state__in=(
                    OrderStates.PENDING_CONSUMER,
                    OrderStates.PENDING_PROVIDER,
                    OrderStates.EXECUTING,
                ),
            )
            .filter(
                Q(attributes__new_options__isnull=False)
                | Q(attributes__old_options__isnull=False)
            )
            .exists()
        ):
            raise IncorrectStateException(
                _("There's a pending order for changing resource options.")
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
        from waldur_mastermind.marketplace.enums import (
            SLURM_OFFERING as SLURM_PLUGIN_NAME,
        )

        if (
            instance.plan_period is None
            or instance.plan_period.plan.offering.type != SLURM_PLUGIN_NAME
        ):
            return instance.usage

        converted_usage = convert_slurm_usage(instance.usage, instance.component.type)
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
        from waldur_mastermind.marketplace.enums import (
            SLURM_OFFERING as SLURM_PLUGIN_NAME,
        )

        # The first check ensures that the second one doesn't fail is the plan period is None
        if (
            instance.component_usage.plan_period is None
            or instance.component_usage.plan_period.plan.offering.type
            != SLURM_PLUGIN_NAME
        ):
            return instance.usage

        converted_usage = convert_slurm_usage(
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
    date = serializers.DateTimeField(
        required=False,
        help_text="Date for usage reporting (staff and service providers for limit-based components). If not provided, current date is used.",
    )

    def validate_date(self, value):
        if not value:
            return value

        user = self.context["request"].user
        if user.is_staff:
            return value

        # Get the component usage to check if user is service provider
        component_usage = self.context["view"].get_object()
        resource = component_usage.resource

        # Check if user is service provider for the resource's offering
        if has_permission(
            self.context["request"],
            PermissionEnum.SET_RESOURCE_USAGE,
            resource.offering.customer,
        ):
            # Check if date is in the current billing period
            current_billing_period = core_utils.month_start(timezone.now())
            date_billing_period = core_utils.month_start(value)

            # If date is in a past billing period (historical backfilling),
            # only allow for limit-based components
            if date_billing_period < current_billing_period:
                if component_usage.component.billing_type != BillingTypes.LIMIT:
                    raise serializers.ValidationError(
                        _(
                            "Service providers can only specify date for limit-based billing components when backfilling past billing periods."
                        )
                    )
            # If date is in current billing period, allow for all component types
            # (service provider is just specifying the measurement timestamp)
            return value

        raise serializers.ValidationError(
            _(
                "Only staff users and service providers can specify date for backfilling."
            )
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
            "date",
        )


class ComponentUserUsageBulkCreateSerializer(serializers.Serializer):
    usages = ComponentUserUsageCreateSerializer(many=True)

    def validate_usages(self, value):
        if not value:
            raise serializers.ValidationError(_("At least one usage item is required."))
        return value


class ComponentUsageMonthlySerializer(
    core_serializers.RestrictedSerializerMixin, serializers.ModelSerializer
):
    offering_uuid = serializers.UUIDField(
        source="component.offering.uuid", read_only=True
    )
    offering_name = serializers.CharField(
        source="component.offering.name", read_only=True
    )
    offering_type = serializers.CharField(
        source="component.offering.type", read_only=True
    )
    service_provider_uuid = serializers.UUIDField(
        source="component.offering.customer.uuid", read_only=True
    )
    service_provider_name = serializers.CharField(
        source="component.offering.customer.name", read_only=True
    )
    category_uuid = serializers.UUIDField(
        source="component.offering.category.uuid", read_only=True
    )
    category_title = serializers.CharField(
        source="component.offering.category.title", read_only=True
    )
    component_type = serializers.CharField(source="component.type", read_only=True)
    component_name = serializers.CharField(source="component.name", read_only=True)
    measured_unit = serializers.CharField(
        source="component.measured_unit", read_only=True
    )
    billing_type = serializers.CharField(
        source="component.billing_type", read_only=True
    )
    limit_period = serializers.CharField(
        source="component.limit_period", read_only=True
    )
    limit_amount = serializers.IntegerField(
        source="component.limit_amount", read_only=True
    )
    billing_period = serializers.DateField()

    class Meta:
        model = models.ComponentUsageMonthly
        fields = (
            "offering_uuid",
            "offering_name",
            "offering_type",
            "service_provider_uuid",
            "service_provider_name",
            "category_uuid",
            "category_title",
            "component_type",
            "component_name",
            "measured_unit",
            "billing_type",
            "limit_amount",
            "limit_period",
            "billing_period",
            "total_consumed",
            "total_allocated",
            "usage_percent",
        )


class ResourcePlanPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ResourcePlanPeriod
        fields = ("uuid", "plan_name", "plan_uuid", "start", "end", "components")

    plan_name = serializers.ReadOnlyField(source="plan.name")
    plan_uuid = serializers.UUIDField(read_only=True, source="plan.uuid")
    components = BaseComponentUsageSerializer(source="current_components", many=True)


class ImportResourceSerializer(serializers.Serializer):
    backend_id = serializers.CharField(help_text="Backend identifier of the resource")
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.available_objects.all(),
        slug_field="uuid",
        help_text="Target project for the resource",
    )
    plan = serializers.SlugRelatedField(
        queryset=models.Plan.objects.all(), slug_field="uuid", required=False
    )
    # The field contains optional data required for importing resources in different plugins
    # For example, tenant_uuid is expected for successful linking cluster to an OpenStack tenant
    additional_details = serializers.JSONField(
        required=False,
        allow_null=True,
        default={},
        write_only=True,
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
        queryset=structure_models.Customer.objects.all(),
        slug_field="uuid",
        help_text="Service provider customer UUID",
    )
    data = serializers.CharField(
        help_text="JWT-encoded data signed with the service provider's API secret code"
    )
    dry_run = serializers.BooleanField(
        default=False,
        required=False,
        help_text="If true, validates the signature without executing the operation",
    )

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
    type = serializers.CharField(help_text="Type of the component")
    amount = serializers.DecimalField(
        decimal_places=2, max_digits=20, help_text="Usage amount"
    )
    description = serializers.CharField(
        required=False, allow_blank=True, help_text="Optional description of usage"
    )
    recurring = serializers.BooleanField(
        default=False, help_text="Whether this usage is recurring"
    )


class ComponentUsageCreateSerializer(serializers.Serializer):
    usages = ComponentUsageItemSerializer(
        many=True, help_text="List of component usage items to report"
    )
    plan_period = serializers.SlugRelatedField(
        queryset=models.ResourcePlanPeriod.objects.all(),
        slug_field="uuid",
        required=False,
        help_text="UUID of the specific resource plan period for usage reporting",
    )
    resource = serializers.SlugRelatedField(
        queryset=models.Resource.objects.all(),
        slug_field="uuid",
        required=False,
        help_text="UUID of the resource for usage reporting (required if plan_period not provided)",
    )
    date = serializers.DateTimeField(
        required=False,
        help_text="Date for usage reporting (staff and service providers for limit-based components). If not provided, current date is used.",
    )

    def _is_limit_based_component_usage(self, attrs):
        """Check if ALL of the usage components are limit-based."""
        plan_period = attrs.get("plan_period")
        resource = plan_period and plan_period.resource or attrs.get("resource")
        if not resource:
            return False

        components_map = self.get_components_map(resource.plan.offering)
        for usage in attrs.get("usages", []):
            component = components_map.get(usage.get("type"))
            if component and component.billing_type != BillingTypes.LIMIT:
                return False
        return True

    def validate_date(self, value):
        if not value:
            return value

        user = self.context["request"].user
        if user.is_staff:
            return value

        # Defer validation for service providers until we have access to the resource
        # in the main validate method
        return value

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

        if not resource.plan:
            raise rf_exceptions.ValidationError(
                {"resource": _("Resource must have a plan to report usage.")}
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

        # Validate date field for service providers
        date_value = attrs.get("date")
        if date_value:
            # Prevent submitting usage for future dates
            now = timezone.now()
            if date_value > now:
                raise rf_exceptions.ValidationError(
                    {
                        "date": _(
                            "Cannot submit usage for future dates. Date must be current date or earlier."
                        )
                    }
                )

        if date_value and not self.context["request"].user.is_staff:
            # Check if user is service provider for the resource's offering
            if has_permission(
                self.context["request"],
                PermissionEnum.SET_RESOURCE_USAGE,
                resource.offering.customer,
            ):
                # Check if date is in the current billing period
                current_billing_period = core_utils.month_start(timezone.now())
                date_billing_period = core_utils.month_start(date_value)

                # If date is in a past billing period (historical backfilling),
                # only allow for limit-based components
                if date_billing_period < current_billing_period:
                    if not self._is_limit_based_component_usage(attrs):
                        raise rf_exceptions.ValidationError(
                            {
                                "date": _(
                                    "Service providers can only specify date for limit-based billing components when backfilling past billing periods."
                                )
                            }
                        )
                # If date is in current billing period, allow for all component types
                # (service provider is just specifying the measurement timestamp)
            else:
                raise rf_exceptions.ValidationError(
                    {
                        "date": _(
                            "Only staff users and service providers can specify date for backfilling."
                        )
                    }
                )

        return attrs

    def save(self):
        plan_period = self.validated_data.get("plan_period")
        resource = (
            plan_period and plan_period.resource or self.validated_data.get("resource")
        )

        components_map = self.get_components_map(resource.plan.offering)
        now = self.validated_data.get("date", timezone.now())
        local_now = timezone.localtime(now)
        billing_period = core_utils.month_start(local_now)
        user: User = self.context["request"].user
        if user.is_anonymous:
            user = None

        for usage in self.validated_data["usages"]:
            amount = usage["amount"]
            description = usage.get("description", "")
            component = components_map[usage["type"]]
            recurring = usage["recurring"]
            if component.billing_type == BillingTypes.USAGE:
                component.validate_amount(resource, amount, now)
            models.ComponentUsage.objects.filter(
                resource=resource,
                component=component,
                billing_period=billing_period,
            ).update(recurring=False)

            if not plan_period:
                plan_period = utils.get_plan_period(resource, now)

            # Look up by (resource, component, billing_period) only —
            # plan_period is a mutable attribute, not part of the identity.
            # This prevents duplicates when plan_period changes from None
            # to a real value (e.g. after historical backfill).
            existing_qs = models.ComponentUsage.objects.filter(
                resource=resource,
                component=component,
                billing_period=billing_period,
            )
            existing = existing_qs.first()
            if existing:
                # Clean up legacy duplicates (same billing period, different plan_periods)
                existing_qs.exclude(pk=existing.pk).delete()
                existing.plan_period = plan_period
                existing.usage = amount
                existing.date = now
                existing.description = description
                existing.recurring = recurring
                existing.modified_by = user
                existing.save()
                usage = existing
                created = False
            else:
                usage = models.ComponentUsage.objects.create(
                    resource=resource,
                    component=component,
                    plan_period=plan_period,
                    billing_period=billing_period,
                    usage=amount,
                    date=now,
                    description=description,
                    recurring=recurring,
                    modified_by=user,
                )
                created = True
            if created:
                logger.info(
                    f"Usage has been created for {resource}, component: {component.type}, value: {amount}"
                )
            else:
                logger.info(
                    f"Usage has been updated for {resource}, component: {component.type}, value: {amount}"
                )


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
    """
    Serializer for OfferingUser that exposes user attributes based on
    per-offering configuration (OfferingUserAttributeConfig).

    All user attribute fields are defined in the schema for SDK generation.
    At runtime, fields are filtered based on the offering's configuration,
    supporting GDPR compliance by exposing only declared personal data.
    """

    offering = serializers.HyperlinkedRelatedField(
        queryset=models.Offering.objects.all(),
        view_name="marketplace-provider-offering-detail",
        lookup_field="uuid",
        required=False,
    )
    offering_uuid = serializers.SlugRelatedField(
        queryset=models.Offering.objects.all(), slug_field="uuid", required=False
    )
    offering_name = serializers.ReadOnlyField(source="offering.name")
    user = serializers.HyperlinkedRelatedField(
        queryset=User.objects.all(),
        view_name="user-detail",
        lookup_field="uuid",
        required=False,
    )
    user_uuid = serializers.SlugRelatedField(
        queryset=User.objects.all(), slug_field="uuid", required=False
    )

    # Core user attributes (controlled by OfferingUserAttributeConfig)
    user_username = serializers.ReadOnlyField(source="user.username")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_first_name = serializers.ReadOnlyField(source="user.first_name")
    user_last_name = serializers.ReadOnlyField(source="user.last_name")
    user_email = serializers.ReadOnlyField(source="user.email")

    # Extended profile attributes
    user_phone_number = serializers.ReadOnlyField(source="user.phone_number")
    user_organization = serializers.ReadOnlyField(source="user.organization")
    user_job_title = serializers.ReadOnlyField(source="user.job_title")
    user_affiliations = serializers.ReadOnlyField(source="user.affiliations")

    # User profile attributes
    user_gender = serializers.ReadOnlyField(source="user.gender")
    user_personal_title = serializers.ReadOnlyField(source="user.personal_title")
    user_place_of_birth = serializers.ReadOnlyField(source="user.place_of_birth")
    user_country_of_residence = serializers.ReadOnlyField(
        source="user.country_of_residence"
    )
    user_nationality = serializers.ReadOnlyField(source="user.nationality")
    user_nationalities = serializers.ReadOnlyField(source="user.nationalities")
    user_organization_country = serializers.ReadOnlyField(
        source="user.organization_country"
    )
    user_organization_type = serializers.ReadOnlyField(source="user.organization_type")
    user_organization_registry_code = serializers.ReadOnlyField(
        source="user.organization_registry_code"
    )
    user_eduperson_assurance = serializers.ReadOnlyField(
        source="user.eduperson_assurance"
    )

    # Legal and identity attributes
    user_civil_number = serializers.ReadOnlyField(source="user.civil_number")
    user_birth_date = serializers.ReadOnlyField(source="user.birth_date")
    user_identity_source = serializers.ReadOnlyField(source="user.identity_source")

    # Identity Bridge attributes
    user_active_isds = serializers.ReadOnlyField(source="user.active_isds")

    customer_uuid = serializers.UUIDField(
        read_only=True, source="offering.customer.uuid"
    )
    customer_name = serializers.ReadOnlyField(source="offering.customer.name")
    is_restricted = serializers.ReadOnlyField()
    state = serializers.SerializerMethodField()
    service_provider_comment = serializers.ReadOnlyField()
    service_provider_comment_url = serializers.ReadOnlyField()
    has_consent = serializers.SerializerMethodField()
    requires_reconsent = serializers.SerializerMethodField()
    has_compliance_checklist = serializers.SerializerMethodField()
    consent_data = serializers.SerializerMethodField()
    is_profile_complete = serializers.SerializerMethodField()
    missing_profile_attributes = serializers.SerializerMethodField()

    # Extra serializer fields exposed/hidden together with their parent attribute.
    # When expose_full_name is enabled, first/last name are also exposed.
    USER_ATTRIBUTE_EXTRA_FIELDS = {
        "full_name": {"user_first_name", "user_last_name"},
    }

    USER_ATTRIBUTE_FIELD_MAP = {
        "username": "user_username",
        "full_name": "user_full_name",
        "email": "user_email",
        "phone_number": "user_phone_number",
        "organization": "user_organization",
        "job_title": "user_job_title",
        "affiliations": "user_affiliations",
        "gender": "user_gender",
        "personal_title": "user_personal_title",
        "place_of_birth": "user_place_of_birth",
        "country_of_residence": "user_country_of_residence",
        "nationality": "user_nationality",
        "nationalities": "user_nationalities",
        "organization_country": "user_organization_country",
        "organization_type": "user_organization_type",
        "organization_registry_code": "user_organization_registry_code",
        "eduperson_assurance": "user_eduperson_assurance",
        "civil_number": "user_civil_number",
        "birth_date": "user_birth_date",
        "identity_source": "user_identity_source",
        "active_isds": "user_active_isds",
    }

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
            # Core user attributes
            "user_username",
            "user_full_name",
            "user_first_name",
            "user_last_name",
            "user_email",
            # Extended profile attributes
            "user_phone_number",
            "user_organization",
            "user_job_title",
            "user_affiliations",
            # User profile attributes
            "user_gender",
            "user_personal_title",
            "user_place_of_birth",
            "user_country_of_residence",
            "user_nationality",
            "user_nationalities",
            "user_organization_country",
            "user_organization_type",
            "user_organization_registry_code",
            "user_eduperson_assurance",
            # Legal and identity attributes
            "user_civil_number",
            "user_birth_date",
            "user_identity_source",
            # Identity Bridge attributes
            "user_active_isds",
            # Other fields
            "created",
            "modified",
            "customer_uuid",
            "customer_name",
            "is_restricted",
            "state",
            "service_provider_comment",
            "service_provider_comment_url",
            "has_consent",
            "requires_reconsent",
            "has_compliance_checklist",
            "consent_data",
            "is_profile_complete",
            "missing_profile_attributes",
        )
        extra_kwargs = dict(
            url={
                "lookup_field": "uuid",
                "view_name": "marketplace-offering-user-detail",
            },
        )

    @extend_schema_field(serializers.ChoiceField(choices=OfferingUserStates.VALUES))
    def get_state(self, offering_user: models.OfferingUser) -> OfferingUserStatesType:
        return offering_user.get_state_display()

    def to_internal_value(self, data):
        # Pre-process data to convert UUID fields to URL fields before field validation
        if self.instance is None:  # Only for creation
            data = data.copy() if hasattr(data, "copy") else dict(data)

            for url_field, uuid_field in (
                ("offering", "offering_uuid"),
                ("user", "user_uuid"),
            ):
                url_provided = url_field in data and data[url_field] is not None
                uuid_provided = uuid_field in data and data[uuid_field] is not None

                if url_provided and uuid_provided:
                    raise serializers.ValidationError(
                        {
                            "non_field_errors": [
                                f"Cannot specify both '{url_field}' URL and '{uuid_field}'. Use one or the other."
                            ]
                        }
                    )

                if not url_provided and not uuid_provided:
                    raise serializers.ValidationError(
                        {
                            "non_field_errors": [
                                f"Either '{url_field}' URL or '{uuid_field}' is required."
                            ]
                        }
                    )

                # If UUID field is provided, convert to URL field format
                if uuid_provided and not url_provided:
                    # Get the field instance to resolve the UUID to the actual object
                    uuid_field_instance = self.fields[uuid_field]
                    url_field_instance = self.fields[url_field]
                    try:
                        # Resolve the UUID to the actual object
                        obj = uuid_field_instance.to_internal_value(data[uuid_field])
                        # Generate the URL for this object
                        request = self.context.get("request")
                        url = url_field_instance.get_url(
                            obj, url_field_instance.view_name, request, format=None
                        )
                        # Remove UUID field and set the URL
                        data.pop(uuid_field)
                        data[url_field] = url
                    except Exception as e:
                        raise serializers.ValidationError({uuid_field: str(e)})

        return super().to_internal_value(data)

    def _has_active_terms_of_service(self, offering) -> bool:
        """Check if offering has active ToS using prefetched data."""
        # Use prefetched terms_of_service_configs to avoid N+1 query
        if (
            hasattr(offering, "_prefetched_objects_cache")
            and "terms_of_service_configs" in offering._prefetched_objects_cache
        ):
            return any(tos.is_active for tos in offering.terms_of_service_configs.all())
        # Fall back to model method if not prefetched
        return offering.has_terms_of_service()

    def _get_active_tos(self, offering):
        """Get active ToS using prefetched data."""
        # Use prefetched terms_of_service_configs to avoid N+1 query
        if (
            hasattr(offering, "_prefetched_objects_cache")
            and "terms_of_service_configs" in offering._prefetched_objects_cache
        ):
            for tos in offering.terms_of_service_configs.all():
                if tos.is_active:
                    return tos
            return None
        # Fall back to query if not prefetched
        return offering.terms_of_service_configs.filter(is_active=True).first()

    def _get_user_consent(self, offering, user):
        """Get user consent using prefetched data."""
        # Use prefetched user_consents to avoid N+1 query
        if (
            hasattr(offering, "_prefetched_objects_cache")
            and "user_consents" in offering._prefetched_objects_cache
        ):
            for consent in offering.user_consents.all():
                if consent.user_id == user.id and consent.revocation_date is None:
                    return consent
            return None
        # Fall back to model method if not prefetched
        return offering.check_user_consent(user)

    def _get_enforce_consent_config(self) -> bool:
        """Get ENFORCE_USER_CONSENT_FOR_OFFERINGS config with request-level caching."""
        request = self.context.get("request")
        if request and hasattr(request, "_enforce_user_consent_cached"):
            return request._enforce_user_consent_cached
        value = config.ENFORCE_USER_CONSENT_FOR_OFFERINGS
        if request:
            request._enforce_user_consent_cached = value
        return value

    def get_has_consent(self, obj) -> bool:
        """Check if the user has active consent for this offering."""
        if not self._has_active_terms_of_service(obj.offering):
            return False
        consent = self._get_user_consent(obj.offering, obj.user)
        return consent is not None

    def get_requires_reconsent(self, obj) -> bool:
        """Check if the user needs to re-consent due to ToS changes."""
        consent = self._get_user_consent(obj.offering, obj.user)
        if not consent:
            return False

        active_tos = self._get_active_tos(obj.offering)
        if not active_tos or not active_tos.requires_reconsent:
            return False
        return active_tos.version != consent.version

    def get_has_compliance_checklist(self, offering_user) -> bool:
        """Check if the offering user has a connected compliance checklist completion."""
        # Check if the offering has compliance requirements
        if not offering_user.offering.compliance_checklist:
            return False

        # Use cached data if available to avoid N+1 queries
        if hasattr(offering_user, "_compliance_completion_exists"):
            return offering_user._compliance_completion_exists

        # Fall back to individual query if not pre-loaded
        from django.contrib.contenttypes.models import ContentType

        offering_user_ct = ContentType.objects.get_for_model(offering_user)
        return checklist_models.ChecklistCompletion.objects.filter(
            scope_content_type=offering_user_ct,
            scope_object_id=offering_user.id,
            checklist=offering_user.offering.compliance_checklist,
        ).exists()

    @extend_schema_field(
        serializers.DictField(
            allow_null=True,
            child=serializers.CharField(),
            help_text="User consent data including uuid, version, and agreement_date",
        )
    )
    def get_consent_data(self, obj):
        """Get the user's consent data for this offering."""
        if not self._get_enforce_consent_config():
            return None

        if not self._has_active_terms_of_service(obj.offering):
            return None

        # Use prefetched user_consents - note: we need any consent for the user,
        # not just active ones, so we iterate through all and find by user
        consent = None
        if (
            hasattr(obj.offering, "_prefetched_objects_cache")
            and "user_consents" in obj.offering._prefetched_objects_cache
        ):
            for c in obj.offering.user_consents.all():
                if c.user_id == obj.user.id:
                    consent = c
                    break
        else:
            consent = obj.offering.user_consents.filter(user=obj.user).first()
        if not consent:
            return None

        return {
            "uuid": str(consent.uuid),
            "version": consent.version,
            "agreement_date": (
                consent.agreement_date.isoformat() if consent.agreement_date else None
            ),
        }

    def _get_missing_attributes(self, obj):
        """Get missing profile attributes for this offering user, with caching."""
        exposed_attributes = self._get_exposed_attributes_cached(obj.offering)
        return utils.get_missing_profile_attributes(obj.user, exposed_attributes)

    @extend_schema_field(serializers.BooleanField())
    def get_is_profile_complete(self, obj) -> bool:
        """Whether the user has filled all exposed attributes for this offering."""
        return len(self._get_missing_attributes(obj)) == 0

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_missing_profile_attributes(self, obj) -> list[str]:
        """List of attribute names the user still needs to fill in."""
        return self._get_missing_attributes(obj)

    def get_fields(self):
        """
        Filter user attribute fields based on OfferingUserAttributeConfig.

        During schema generation (swagger_fake_view), all fields are included.
        At runtime, only fields enabled in the offering's config are included.
        """
        request = self.context["request"]
        fields = super().get_fields()

        # Handle UUID field conversion for safe methods
        if request.method in SAFE_METHODS:
            if "user_uuid" in fields:
                fields["user_uuid"] = serializers.UUIDField(source="user.uuid")
            if "offering_uuid" in fields:
                fields["offering_uuid"] = serializers.UUIDField(source="offering.uuid")

        # Skip attribute filtering during schema generation - show all fields in OpenAPI/SDK
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        # For detail views with a single instance, filter fields at schema level
        # For list views, filtering happens in to_representation() per-instance
        if self.instance and not isinstance(self.instance, list):
            # Get exposed attributes from the offering's config
            exposed_attributes = (
                models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(
                    self.instance.offering
                )
            )

            # Get serializer field names to keep
            exposed_serializer_fields = {
                self.USER_ATTRIBUTE_FIELD_MAP[attr]
                for attr in exposed_attributes
                if attr in self.USER_ATTRIBUTE_FIELD_MAP
            }
            # Include extra fields linked to exposed attributes
            for attr in exposed_attributes:
                extra = self.USER_ATTRIBUTE_EXTRA_FIELDS.get(attr)
                if extra:
                    exposed_serializer_fields.update(extra)

            # Remove user attribute fields that are not exposed
            all_user_attribute_fields = set(self.USER_ATTRIBUTE_FIELD_MAP.values())
            for extra in self.USER_ATTRIBUTE_EXTRA_FIELDS.values():
                all_user_attribute_fields.update(extra)
            fields_to_remove = all_user_attribute_fields - exposed_serializer_fields

            for field_name in fields_to_remove:
                if field_name in fields:
                    del fields[field_name]

        return fields

    def _get_default_offering_user_attributes_cached(self):
        """Get DEFAULT_OFFERING_USER_ATTRIBUTES config with request-level caching."""
        request = self.context.get("request")
        if request and hasattr(request, "_default_offering_user_attrs_cached"):
            return request._default_offering_user_attrs_cached
        value = config.DEFAULT_OFFERING_USER_ATTRIBUTES or [
            "username",
            "full_name",
            "email",
        ]
        if request:
            request._default_offering_user_attrs_cached = value
        return value

    def _get_exposed_attributes_cached(self, offering):
        """Get exposed attributes for an offering with request-level caching."""
        request = self.context.get("request")
        cache_key = f"_offering_exposed_attrs_{offering.id}"

        if request and hasattr(request, cache_key):
            return getattr(request, cache_key)

        # Try to get from offering's config, fallback to cached default
        try:
            exposed_attributes = offering.user_attribute_config.get_exposed_fields()
        except models.OfferingUserAttributeConfig.DoesNotExist:
            exposed_attributes = self._get_default_offering_user_attributes_cached()

        if request:
            setattr(request, cache_key, exposed_attributes)

        return exposed_attributes

    def to_representation(self, instance):
        """
        Filter user attributes based on offering config during serialization.

        This ensures proper filtering for list views where different offerings
        may have different attribute configurations.
        """
        data = super().to_representation(instance)

        # Skip filtering during schema generation
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return data

        # Get exposed attributes for this specific offering (cached per request)
        exposed_attributes = self._get_exposed_attributes_cached(instance.offering)

        # Get serializer field names that should be exposed
        exposed_serializer_fields = {
            self.USER_ATTRIBUTE_FIELD_MAP[attr]
            for attr in exposed_attributes
            if attr in self.USER_ATTRIBUTE_FIELD_MAP
        }
        # Include extra fields linked to exposed attributes
        for attr in exposed_attributes:
            extra = self.USER_ATTRIBUTE_EXTRA_FIELDS.get(attr)
            if extra:
                exposed_serializer_fields.update(extra)

        # Remove non-exposed user attribute fields from the output
        all_user_attribute_fields = set(self.USER_ATTRIBUTE_FIELD_MAP.values())
        for extra in self.USER_ATTRIBUTE_EXTRA_FIELDS.values():
            all_user_attribute_fields.update(extra)
        fields_to_remove = all_user_attribute_fields - exposed_serializer_fields

        for field_name in fields_to_remove:
            if field_name in data:
                del data[field_name]

        return data

    def create(self, validated_data):
        request = self.context["request"]
        offering: models.Offering = validated_data["offering"]

        if not has_permission(
            request, PermissionEnum.CREATE_OFFERING_USER, offering.customer
        ):
            raise rf_exceptions.PermissionDenied()

        if not offering.plugin_options.get("service_provider_can_create_offering_user"):
            raise rf_exceptions.ValidationError(
                _("It is not allowed to create users for current offering.")
            )

        instance = super().create(validated_data)

        # Set state to OK for backward compatibility when username is provided during creation
        if (
            instance.username
            and instance.state == OfferingUserStates.CREATION_REQUESTED
        ):
            instance.set_ok()
            instance.save(update_fields=["state"])

        return instance

    def update(self, instance: models.OfferingUser, validated_data):
        request = self.context["request"]
        offering = instance.offering

        if not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, offering.customer
        ) and not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, offering
        ):
            raise rf_exceptions.PermissionDenied()

        instance = super().update(instance, validated_data)

        return instance


class OfferingUserUpdateRestrictionSerializer(serializers.Serializer):
    is_restricted = serializers.BooleanField(
        help_text="Whether the offering user should be restricted from accessing resources"
    )

    def validate(self, attrs):
        request = self.context["request"]
        offering_user = self.instance
        offering = offering_user.offering
        if not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, offering.customer
        ) and not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, offering
        ):
            raise rf_exceptions.PermissionDenied()
        return attrs


class OfferingUserStateTransitionSerializer(serializers.Serializer):
    comment = core_serializers.HTMLCleanField(
        required=False,
        allow_blank=True,
        help_text="Comment explaining the state transition",
    )
    comment_url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text="URL reference related to the state transition comment",
    )

    def validate(self, attrs):
        request = self.context["request"]
        offering_user = self.instance
        offering = offering_user.offering

        if not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, offering.customer
        ) and not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, offering
        ):
            raise rf_exceptions.PermissionDenied()

        return attrs


class OfferingUserServiceProviderCommentSerializer(serializers.ModelSerializer):
    """Serializer for service providers to update comment fields."""

    service_provider_comment = core_serializers.HTMLCleanField(
        required=False, allow_blank=True
    )

    class Meta:
        model = models.OfferingUser
        fields = ("service_provider_comment", "service_provider_comment_url")


class ProfileFieldWarningOfferingSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField()
    offering_name = serializers.CharField()


class ProfileFieldWarningsSerializer(serializers.Serializer):
    """Response serializer for profile_field_warnings endpoint.

    Returns a mapping of user field names to lists of offerings
    that expose those fields via OfferingUserAttributeConfig.
    """

    # The actual response is a dict with dynamic keys (field names),
    # each mapping to a list of {offering_uuid, offering_name}.
    # DRF's Serializer is used here for OpenAPI schema documentation.


class UserChecklistCompletionSerializer(serializers.ModelSerializer):
    """Serializer for checklist completions associated with user's offering users."""

    offering_user = OfferingUserSerializer(read_only=True)
    offering_user_uuid = serializers.SerializerMethodField()
    offering_name = serializers.SerializerMethodField()
    offering_uuid = serializers.SerializerMethodField()
    customer_provider_uuid = serializers.SerializerMethodField()
    customer_provider_name = serializers.SerializerMethodField()
    checklist_name = serializers.CharField(source="checklist.name", read_only=True)
    checklist_uuid = serializers.CharField(source="checklist.uuid", read_only=True)
    checklist_description = serializers.CharField(
        source="checklist.description", read_only=True
    )
    completion_percentage = serializers.SerializerMethodField()
    unanswered_required_questions = serializers.SerializerMethodField()

    class Meta:
        model = checklist_models.ChecklistCompletion
        fields = (
            "uuid",
            "offering_user",
            "offering_user_uuid",
            "offering_name",
            "offering_uuid",
            "customer_provider_uuid",
            "customer_provider_name",
            "checklist_uuid",
            "checklist_name",
            "checklist_description",
            "is_completed",
            "completion_percentage",
            "unanswered_required_questions",
            "requires_review",
            "reviewed_by",
            "reviewed_at",
            "review_notes",
            "created",
            "modified",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_offering_user_uuid(self, obj):
        """Get the UUID of the associated OfferingUser."""
        # Use cached data if available to avoid N+1 queries
        if hasattr(obj, "_offering_user_cache") and obj._offering_user_cache:
            return str(obj._offering_user_cache.uuid)

        # Fallback to database query (should be rare with optimization)
        try:
            offering_user = models.OfferingUser.objects.get(id=obj.scope_object_id)
            return str(offering_user.uuid)
        except models.OfferingUser.DoesNotExist:
            return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_offering_name(self, obj):
        """Get the name of the offering."""
        # Use cached data if available to avoid N+1 queries
        if hasattr(obj, "_offering_user_cache") and obj._offering_user_cache:
            return obj._offering_user_cache.offering.name

        # Fallback to database query (should be rare with optimization)
        try:
            offering_user = models.OfferingUser.objects.get(id=obj.scope_object_id)
            return offering_user.offering.name
        except models.OfferingUser.DoesNotExist:
            return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_offering_uuid(self, obj):
        """Get the UUID of the offering."""
        # Use cached data if available to avoid N+1 queries
        if hasattr(obj, "_offering_user_cache") and obj._offering_user_cache:
            return str(obj._offering_user_cache.offering.uuid)

        # Fallback to database query (should be rare with optimization)
        try:
            offering_user = models.OfferingUser.objects.get(id=obj.scope_object_id)
            return str(offering_user.offering.uuid)
        except models.OfferingUser.DoesNotExist:
            return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_customer_provider_uuid(self, obj):
        """Get the UUID of the provider customer."""
        # Use cached data if available to avoid N+1 queries
        if hasattr(obj, "_offering_user_cache") and obj._offering_user_cache:
            return str(obj._offering_user_cache.offering.customer.uuid)

        # Fallback to database query (should be rare with optimization)
        try:
            offering_user = models.OfferingUser.objects.get(id=obj.scope_object_id)
            return str(offering_user.offering.customer.uuid)
        except models.OfferingUser.DoesNotExist:
            return None

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_customer_provider_name(self, obj):
        """Get the name of the provider customer."""
        # Use cached data if available to avoid N+1 queries
        if hasattr(obj, "_offering_user_cache") and obj._offering_user_cache:
            return obj._offering_user_cache.offering.customer.name

        # Fallback to database query (should be rare with optimization)
        try:
            offering_user = models.OfferingUser.objects.get(id=obj.scope_object_id)
            return offering_user.offering.customer.name
        except models.OfferingUser.DoesNotExist:
            return None

    @extend_schema_field(serializers.FloatField(min_value=0, max_value=100))
    def get_completion_percentage(self, obj):
        """Calculate the completion percentage of the checklist."""
        total = obj.answers.count()
        if total == 0:
            return 0
        answered = obj.answers.filter(answer_data__isnull=False).count()
        return round((answered / total) * 100, 2)

    @extend_schema_field(serializers.IntegerField(min_value=0))
    def get_unanswered_required_questions(self, obj):
        """Get count of unanswered required questions."""
        required_questions = obj.checklist.questions.filter(required=True)
        answered_questions = obj.answers.filter(
            question__in=required_questions, answer_data__isnull=False
        ).values_list("question_id", flat=True)
        return required_questions.exclude(id__in=answered_questions).count()

    def to_representation(self, instance):
        """Optimized representation using prefetched data."""
        # The ViewSet should have attached _offering_user_cache to avoid N+1 queries
        # If not available, fall back to a single query (but this should be rare)
        if not hasattr(instance, "_offering_user_cache"):
            try:
                instance._offering_user_cache = (
                    models.OfferingUser.objects.select_related("offering", "user").get(
                        id=instance.scope_object_id
                    )
                )
            except models.OfferingUser.DoesNotExist:
                instance._offering_user_cache = None

        data = super().to_representation(instance)

        # Add offering user data if available (this should always be true with optimization)
        if instance._offering_user_cache:
            data["offering_user"] = {
                "uuid": str(instance._offering_user_cache.uuid),
                "username": instance._offering_user_cache.username,
                "user_full_name": instance._offering_user_cache.user.full_name,
                "user_email": instance._offering_user_cache.user.email,
                "state": instance._offering_user_cache.get_state_display(),
                "is_restricted": instance._offering_user_cache.is_restricted,
            }

        return data

    def validate(self, attrs):
        request = self.context["request"]
        offering_user = self.instance
        offering = offering_user.offering

        if not has_permission(
            request, PermissionEnum.UPDATE_OFFERING_USER, offering.customer
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
            "scope_type",
            "scope_type_label",
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

    # Use prefetched data if available to avoid N+1 queries
    if (
        hasattr(customer, "_prefetched_objects_cache")
        and "serviceprovider" in customer._prefetched_objects_cache
    ):
        return bool(customer._prefetched_objects_cache["serviceprovider"])
    else:
        # Fallback to original query behavior
        return models.ServiceProvider.objects.filter(customer=customer).exists()


def add_service_provider(sender, fields, **kwargs):
    """Add a service provider field to the serializer."""
    fields["is_service_provider"] = serializers.SerializerMethodField()
    setattr(sender, "get_is_service_provider", get_is_service_provider)


def add_service_provider_uuid(sender, fields, **kwargs):
    """Add a service provider UUID field to the serializer."""
    fields["service_provider_uuid"] = serializers.SlugRelatedField(
        slug_field="uuid",
        source="serviceprovider",
        read_only=True,
        allow_null=True,
    )


def add_service_provider_url(sender, fields, **kwargs):
    """Add a service provider URL field to the serializer."""
    fields["service_provider"] = serializers.HyperlinkedRelatedField(
        lookup_field="uuid",
        view_name="marketplace-service-provider-detail",
        source="serviceprovider",
        read_only=True,
        allow_null=True,
    )


def get_call_managing_organization_uuid(serializer, scope) -> str | None:
    customer = structure_permissions._get_customer(scope)

    # Use prefetched data if available to avoid N+1 queries
    if (
        hasattr(customer, "_prefetched_objects_cache")
        and "callmanagingorganisation" in customer._prefetched_objects_cache
    ):
        organization = customer._prefetched_objects_cache["callmanagingorganisation"]
        if organization:
            return str(organization.uuid)
        return None
    else:
        # Fallback to original query behavior
        call_managing_organisation = (
            proposal_models.CallManagingOrganisation.objects.filter(customer=customer)
        )
        if call_managing_organisation.exists():
            return str(call_managing_organisation.first().uuid)
        return None


def add_call_managing_organization_uuid(sender, fields, **kwargs):
    """Add a call managing organization UUID field to the serializer."""
    fields["call_managing_organization_uuid"] = serializers.SerializerMethodField()
    setattr(
        sender,
        "get_call_managing_organization_uuid",
        get_call_managing_organization_uuid,
    )


class ResourceTerminateSerializer(serializers.Serializer):
    attributes = serializers.JSONField(
        label=_("Termination attributes"),
        required=False,
        help_text="Optional attributes/parameters to pass to the termination operation",
    )


class ResourceSetStateErredSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Resource
        fields = ("error_message", "error_traceback")
        extra_kwargs = dict(
            error_message={"required": False},
            error_traceback={"required": False},
        )


class ProjectHyperlinkSerializer(serializers.Serializer):
    url = serializers.HyperlinkedRelatedField(
        queryset=structure_models.Project.available_objects.all(),
        view_name="project-detail",
        lookup_field="uuid",
    )

    def to_internal_value(self, data):
        return super().to_internal_value(data)["url"]


class MoveResourceSerializer(serializers.Serializer):
    project = ProjectHyperlinkSerializer(
        write_only=True,
        help_text="Target project URL where the resource should be moved",
    )


class ResourceSetLimitsSerializer(serializers.Serializer):
    limits = serializers.JSONField(
        help_text="Dictionary mapping component types to their new limit values"
    )

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
    # Check for prefetched data from ProjectViewSet.list()
    request = serializer.context.get("request")
    if request and hasattr(request, "_marketplace_resource_counts"):
        return request._marketplace_resource_counts.get(project.id, {})

    # Fallback to per-project query when prefetched data is not available
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
    """Add a marketplace resource count field to the serializer."""
    fields["marketplace_resource_count"] = serializers.SerializerMethodField()
    setattr(sender, "get_marketplace_resource_count", get_marketplace_resource_count)


core_signals.pre_serializer_fields.connect(
    sender=structure_serializers.ProjectSerializer,
    receiver=add_marketplace_resource_count,
)


class OfferingThumbnailSerializer(serializers.HyperlinkedModelSerializer):
    thumbnail = serializers.FileField(required=True, validators=[ImageValidator])

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


class TagsSerializer(serializers.Serializer):
    tags = serializers.SlugRelatedField(
        queryset=models.Tag.objects.all(),
        slug_field="uuid",
        required=False,
        many=True,
    )

    def save(self, **kwargs):
        offering = self.instance
        tags = self.validated_data["tags"]
        offering.tags.clear()

        if tags:
            offering.tags.add(*tags)


class ProviderOfferingCostsSerializer(serializers.Serializer):
    period = serializers.SerializerMethodField(help_text="Billing period (YYYY-MM)")
    price = serializers.SerializerMethodField(help_text="Price amount excluding tax")
    tax = serializers.SerializerMethodField(help_text="Tax amount")
    total = serializers.SerializerMethodField(help_text="Total amount including tax")

    def get_period(self, record) -> str:
        return "%s-%02d" % (record["invoice__year"], record["invoice__month"])

    def get_total(self, record) -> float:
        return round(record["computed_tax"] + record["computed_price"], 2)

    def get_price(self, record) -> float:
        return round(record["computed_price"], 2)

    def get_tax(self, record) -> float:
        return round(record["computed_tax"], 2)


class OfferingCostSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(
        source="resource__offering__uuid", help_text="UUID of the offering"
    )
    offering_name = serializers.CharField(
        source="resource__offering__name", help_text="Name of the offering"
    )
    cost = serializers.FloatField(help_text="Total cost for the offering")


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
    name = serializers.SerializerMethodField(help_text="Name from the record")
    uuid = serializers.SerializerMethodField(help_text="UUID from the record")
    count = serializers.SerializerMethodField(help_text="Count value from the record")

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


class OfferingStateCounterSerializer(serializers.Serializer):
    state = serializers.CharField()
    count = serializers.IntegerField()


class OfferingStateCountersSerializer(serializers.Serializer):
    resources = OfferingStateCounterSerializer(many=True)
    users = OfferingStateCounterSerializer(many=True)


class OfferingStatsCounterSerializer(serializers.Serializer):
    category_uuid = serializers.UUIDField(help_text="UUID of the category")
    category_title = serializers.CharField(help_text="Title of the category")
    service_provider_name = serializers.CharField(
        help_text="Name of the service provider"
    )
    service_provider_uuid = serializers.UUIDField(
        help_text="UUID of the service provider"
    )
    count = serializers.IntegerField(help_text="Number of offerings")


class UserNationalityStatsSerializer(serializers.Serializer):
    nationality = serializers.CharField(help_text="Nationality code")
    count = serializers.IntegerField(help_text="Number of users")


class UserResidenceCountryStatsSerializer(serializers.Serializer):
    country_of_residence = serializers.CharField(help_text="Country of residence code")
    count = serializers.IntegerField(help_text="Number of users")


class ProjectCreationTrendSerializer(serializers.Serializer):
    """Monthly creation trend data point."""

    month = serializers.CharField(help_text="Month in YYYY-MM format")
    count = serializers.IntegerField(help_text="Number of items created")


class TopServiceProviderByResourcesSerializer(serializers.Serializer):
    """Service provider ranked by active resource count."""

    customer_uuid = serializers.UUIDField(help_text="UUID of the service provider")
    customer_name = serializers.CharField(help_text="Name of the service provider")
    resources_count = serializers.IntegerField(help_text="Number of active resources")
    projects_count = serializers.IntegerField(help_text="Number of distinct projects")


class MarketplaceCustomerStatsSerializer(CountStatsSerializer):
    abbreviation = serializers.SerializerMethodField(
        help_text="Customer abbreviation from the record"
    )

    def get_abbreviation(self, record) -> str:
        return self._get_value(record, "abbreviation")


class CustomerOecdCodeStatsSerializer(MarketplaceCustomerStatsSerializer):
    oecd = serializers.CharField(source="oecd_fos_2007_name")


class CustomerIndustryFlagStatsSerializer(MarketplaceCustomerStatsSerializer):
    is_industry = serializers.CharField(help_text="Industry classification flag")


class OfferingCountryStatsSerializer(serializers.Serializer):
    country = serializers.CharField(
        source="offering__country", help_text="Country code of the offering"
    )
    count = serializers.IntegerField(help_text="Number of offerings in this country")


class ComponentUsagesStatsSerializer(serializers.Serializer):
    usage = serializers.DecimalField(
        decimal_places=2, max_digits=20, help_text="Total usage amount"
    )
    offering_uuid = serializers.UUIDField(
        source="resource__offering__uuid", help_text="UUID of the offering"
    )
    component_type = serializers.CharField(
        source="component__type", help_text="Type of the component"
    )
    offering_country = serializers.CharField(
        read_only=True, help_text="Country of the offering"
    )
    organization_group_name = serializers.CharField(
        read_only=True, help_text="Name of the organization group"
    )
    organization_group_uuid = serializers.CharField(
        read_only=True, help_text="UUID of the organization group"
    )


class ComponentUsagesPerMonthStatsSerializer(ComponentUsagesStatsSerializer):
    month = serializers.IntegerField(
        source="billing_period__month", help_text="Month of the billing period"
    )
    year = serializers.IntegerField(
        source="billing_period__year", help_text="Year of the billing period"
    )


class ComponentUsagesPerProjectSerializer(serializers.Serializer):
    project_uuid = serializers.UUIDField(help_text="UUID of the project")
    component_type = serializers.CharField(help_text="Type of the component")
    usage = serializers.IntegerField(
        read_only=True, help_text="Total usage for the component"
    )


class BaseServiceProviderStatsSerializer(serializers.Serializer):
    service_provider_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the service provider"
    )
    customer_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the customer"
    )
    customer_name = serializers.CharField(
        read_only=True, help_text="Name of the customer"
    )
    customer_organization_group_uuid = serializers.CharField(
        read_only=True, help_text="UUID of the customer's organization group"
    )
    customer_organization_group_name = serializers.CharField(
        read_only=True, help_text="Name of the customer's organization group"
    )
    count = serializers.IntegerField(read_only=True, help_text="Count value")


class CountUsersOfServiceProvidersSerializer(BaseServiceProviderStatsSerializer):
    pass


class CountProjectsOfServiceProvidersSerializer(BaseServiceProviderStatsSerializer):
    pass


class CountProjectsOfServiceProvidersGroupedByOecdSerializer(
    BaseServiceProviderStatsSerializer
):
    oecd_fos_2007_name = serializers.CharField(read_only=True)


class BaseNestedUsagesSerializer(serializers.Serializer):
    usages = serializers.DictField(
        child=serializers.DictField(
            child=serializers.DecimalField(decimal_places=2, max_digits=20),
        ),
        help_text="Nested dictionary of usage values by category and component type",
    )


class BaseNestedLimitsSerializer(serializers.Serializer):
    limits = serializers.DictField(
        child=serializers.DictField(
            child=serializers.DecimalField(decimal_places=2, max_digits=20),
        ),
        help_text="Nested dictionary of resource limits by category and component type",
    )


class ProjectsUsagesGroupedByOecdSerializer(BaseNestedUsagesSerializer):
    pass


class ProjectsUsagesGroupedByIndustryFlagSerializer(BaseNestedUsagesSerializer):
    pass


class ProjectsLimitsGroupedByOecdSerializer(BaseNestedLimitsSerializer):
    pass


class ProjectsLimitsGroupedByIndustryFlagSerializer(BaseNestedLimitsSerializer):
    pass


class CountUniqueUsersConnectedWithActiveResourcesOfServiceProviderSerializer(
    serializers.Serializer
):
    customer_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the customer"
    )
    customer_name = serializers.CharField(
        read_only=True, help_text="Name of the customer"
    )
    count_users = serializers.IntegerField(
        read_only=True, help_text="Number of unique users"
    )


class ResourcesLimitsSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the offering"
    )
    name = serializers.CharField(read_only=True, help_text="Name of the limit")
    value = serializers.IntegerField(read_only=True, help_text="Limit value")
    offering_country = serializers.CharField(
        read_only=True, help_text="Country of the offering"
    )
    organization_group_name = serializers.CharField(
        read_only=True, help_text="Name of the organization group"
    )
    organization_group_uuid = serializers.CharField(
        read_only=True, help_text="UUID of the organization group"
    )


class OfferingStatsSerializer(serializers.Serializer):
    count = serializers.IntegerField(help_text="Number of resources for the offering")
    name = serializers.CharField(
        source="offering__name", help_text="Name of the offering"
    )
    uuid = serializers.CharField(
        source="offering__uuid", help_text="UUID of the offering"
    )
    country = serializers.CharField(
        source="offering__country", help_text="Country of the offering"
    )


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
    offering_user_state = serializers.SerializerMethodField()

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
            "offering_user_state",
        ]
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }

    def _get_offering_user(self, user):
        offering_users_map = self.context.get("offering_users_map")
        if offering_users_map is not None:
            return offering_users_map.get(user.id)
        offering = self.context["offering"]
        return models.OfferingUser.objects.filter(user=user, offering=offering).first()

    def _get_permission(self, user):
        permissions_map = self.context.get("permissions_map")
        if permissions_map is not None:
            return permissions_map.get(user.id)
        project = self.context["project"]
        return get_permissions(project, user).select_related("role").first()

    def get_offering_user_username(self, user) -> str | None:
        offering_user = self._get_offering_user(user)
        return offering_user.username if offering_user else None

    def get_role(self, user: User) -> str:
        permission = self._get_permission(user)
        return permission and permission.role.name

    def get_expiration_time(self, user: User) -> datetime.datetime | None:
        permission = self._get_permission(user)
        return permission and permission.expiration_time

    @extend_schema_field(
        serializers.ChoiceField(choices=OfferingUserStates.VALUES, allow_null=True)
    )
    def get_offering_user_state(self, user: User) -> OfferingUserStatesType | None:
        offering_user = self._get_offering_user(user)
        return offering_user.get_state_display() if offering_user else None


class MarketplaceServiceProviderUserSerializer(
    core_serializers.RestrictedSerializerMixin, serializers.ModelSerializer
):
    """
    Serializer for users in the service provider users endpoint.

    Applies GDPR-aware attribute filtering based on the union of exposed
    fields from all offerings of the service provider. If at least one
    offering exposes an attribute, it is shown.
    """

    # Extra serializer fields exposed/hidden together with their parent attribute.
    USER_ATTRIBUTE_EXTRA_FIELDS = {
        "full_name": {"first_name", "last_name"},
    }

    # Map attribute names to serializer field names
    # For User model, field names match attribute names (unlike OfferingUserSerializer)
    USER_ATTRIBUTE_FIELD_MAP = {
        "username": "username",
        "full_name": "full_name",
        "email": "email",
        "phone_number": "phone_number",
        "organization": "organization",
        "job_title": "job_title",
        "affiliations": "affiliations",
        "registration_method": "registration_method",
        "gender": "gender",
        "personal_title": "personal_title",
        "place_of_birth": "place_of_birth",
        "country_of_residence": "country_of_residence",
        "nationality": "nationality",
        "nationalities": "nationalities",
        "organization_country": "organization_country",
        "organization_type": "organization_type",
        "organization_registry_code": "organization_registry_code",
        "eduperson_assurance": "eduperson_assurance",
        "civil_number": "civil_number",
        "birth_date": "birth_date",
        "identity_source": "identity_source",
        "active_isds": "active_isds",
    }

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
            # User profile attributes
            "job_title",
            "gender",
            "personal_title",
            "place_of_birth",
            "country_of_residence",
            "nationality",
            "nationalities",
            "organization_country",
            "organization_type",
            "organization_registry_code",
            "eduperson_assurance",
            # Legal and identity attributes
            "civil_number",
            "birth_date",
            "identity_source",
            # Identity Bridge attributes
            "active_isds",
        )

    projects_count = serializers.SerializerMethodField()

    def get_projects_count(self, user) -> int:
        service_provider = self.context["service_provider"]
        projects = utils.get_service_provider_project_ids(service_provider)
        content_type = ContentType.objects.get_for_model(structure_models.Project)
        return UserRole.objects.filter(
            user=user, object_id__in=projects, content_type=content_type, is_active=True
        ).count()

    # Default attributes when no config exists
    DEFAULT_EXPOSED_ATTRIBUTES = ["username", "full_name", "email"]

    def _get_service_provider_exposed_attributes(self):
        """
        Get the union of exposed attributes across all service provider offerings.

        Uses request-level caching to avoid repeated database queries.
        Returns the least restrictive set of attributes (union) — if any offering
        exposes a field, it is shown.
        """
        # Check if this is schema generation context - return ALL possible attributes
        # so the OpenAPI schema shows the maximum possible response shape
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return list(self.USER_ATTRIBUTE_FIELD_MAP.keys())

        request = self.context.get("request")
        service_provider = self.context.get("service_provider")

        if not service_provider:
            # Fallback to default if no service provider in context
            return (
                config.DEFAULT_OFFERING_USER_ATTRIBUTES
                or self.DEFAULT_EXPOSED_ATTRIBUTES
            )

        cache_key = f"_sp_exposed_attrs_{service_provider.id}"
        if request and hasattr(request, cache_key):
            return getattr(request, cache_key)

        # Get all offerings for this service provider that can have offering users
        offerings = models.Offering.objects.filter(
            customer=service_provider.customer,
            plugin_options__service_provider_can_create_offering_user=True,
        ).prefetch_related("user_attribute_config")

        if not offerings.exists():
            # No offerings, use default
            default_attrs = (
                config.DEFAULT_OFFERING_USER_ATTRIBUTES
                or self.DEFAULT_EXPOSED_ATTRIBUTES
            )
            if request:
                setattr(request, cache_key, default_attrs)
            return default_attrs

        # Pre-fetch the constance default once to avoid N+1 queries
        # when multiple offerings lack a user_attribute_config.
        default_attrs = (
            config.DEFAULT_OFFERING_USER_ATTRIBUTES or self.DEFAULT_EXPOSED_ATTRIBUTES
        )

        # Get union of exposed fields from all offerings
        exposed_sets = []
        for offering in offerings:
            exposed_fields = (
                models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(
                    offering, default_attributes=default_attrs
                )
            )
            exposed_sets.append(set(exposed_fields))

        # Union of all sets (least restrictive — if any offering exposes a field, show it)
        if exposed_sets:
            result = set.union(*exposed_sets)
        else:
            result = set()

        result_list = list(result)
        if request:
            setattr(request, cache_key, result_list)
        return result_list

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        # Remove is_active for non-staff/non-support users
        if (
            user.is_authenticated
            and "is_active" in fields
            and not user.is_staff
            and not user.is_support
        ):
            del fields["is_active"]

        # Apply GDPR-aware attribute filtering
        exposed_attributes = self._get_service_provider_exposed_attributes()

        # Get serializer field names that should be exposed
        exposed_serializer_fields = {
            self.USER_ATTRIBUTE_FIELD_MAP[attr]
            for attr in exposed_attributes
            if attr in self.USER_ATTRIBUTE_FIELD_MAP
        }
        # Include extra fields linked to exposed attributes
        for attr in exposed_attributes:
            extra = self.USER_ATTRIBUTE_EXTRA_FIELDS.get(attr)
            if extra:
                exposed_serializer_fields.update(extra)

        # Remove user attribute fields that are not exposed
        all_user_attribute_fields = set(self.USER_ATTRIBUTE_FIELD_MAP.values())
        for extra in self.USER_ATTRIBUTE_EXTRA_FIELDS.values():
            all_user_attribute_fields.update(extra)
        fields_to_remove = all_user_attribute_fields - exposed_serializer_fields

        for field_name in fields_to_remove:
            if field_name in fields:
                del fields[field_name]

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

        queryset = User.objects.filter(id__in=ids)
        if config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
            # Only users with active consent for ToS-required offerings or use offerings that don't require ToS
            queryset = queryset.filter(
                Q(
                    offering_consents__offering__customer=service_provider.customer,
                    offering_consents__offering__plugin_options__service_provider_can_create_offering_user=True,
                    offering_consents__offering__terms_of_service_configs__is_active=True,
                    offering_consents__revocation_date__isnull=True,
                )
                | Q(
                    id__in=models.OfferingUser.objects.filter(
                        offering__customer=service_provider.customer,
                        offering__plugin_options__service_provider_can_create_offering_user=True,
                        offering__terms_of_service_configs__isnull=True,
                    ).values_list("user_id", flat=True)
                )
            )

        return queryset.distinct()

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
            "thumbnail",
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
    ) -> Literal["Draft", "Active", "Paused", "Archived", "Unavailable"]:
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

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

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


class MoveOfferingSerializer(serializers.Serializer):
    customer = serializers.HyperlinkedRelatedField(
        queryset=structure_models.Customer.objects.all(),
        view_name="customer-detail",
        lookup_field="uuid",
        help_text="Target customer URL with service provider profile where the offering should be moved",
    )
    preserve_permissions = serializers.BooleanField(
        required=True,
        help_text="Whether to preserve existing permissions when moving the offering",
    )

    def validate(self, attrs):
        customer = attrs.get("customer")
        if not models.ServiceProvider.objects.filter(customer=customer).exists():
            raise serializers.ValidationError(
                {"customer": _("Customer must have a service provider profile.")}
            )

        return attrs


class FingerprintSerializer(serializers.Serializer):
    md5 = serializers.CharField(read_only=True, help_text="MD5 fingerprint of SSH key")
    sha256 = serializers.CharField(
        read_only=True, help_text="SHA256 fingerprint of SSH key"
    )
    sha512 = serializers.CharField(
        read_only=True, help_text="SHA512 fingerprint of SSH key"
    )


class BaseServiceAccountSerializer(
    serializers.HyperlinkedModelSerializer, core_serializers.AugmentedSerializerMixin
):
    error_message = serializers.CharField(read_only=True)
    state = serializers.SerializerMethodField()

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
            "state",
        )
        read_only_fields = [
            "backend_id",
            "error_message",
            "error_traceback",
        ]

    @extend_schema_field(serializers.ChoiceField(choices=ServiceAccountState.VALUES))
    def get_state(
        self, service_account: models.BaseServiceAccount
    ) -> ServiceAccountStatesType:
        return service_account.get_state_display()


class BaseScopedServiceAccountSerializer(BaseServiceAccountSerializer):
    token = serializers.SerializerMethodField()
    expires_at = serializers.SerializerMethodField()

    class Meta:
        model = models.ScopedServiceAccount
        fields = BaseServiceAccountSerializer.Meta.fields + (
            "token",
            "email",
            "expires_at",
            "preferred_identifier",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-service-account-detail",
            },
        }

        protected_fields = ("preferred_identifier",)

    def get_token(self, obj) -> str | None:
        if hasattr(obj, "_token"):
            return obj._token
        return None

    def get_expires_at(self, obj) -> str | None:
        if hasattr(obj, "_expires_at"):
            return obj._expires_at
        return None


class ProjectServiceAccountSerializer(BaseScopedServiceAccountSerializer):
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.available_objects.all(),
        slug_field="uuid",
        allow_null=True,
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
        queryset=structure_models.Customer.objects.all(),
        slug_field="uuid",
        allow_null=True,
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
    state = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(choices=RobotAccountStates.VALUES))
    def get_state(
        self, robot_account: models.RobotAccount
    ) -> Literal[
        "Requested",
        "Creating",
        "OK",
        "Requested deletion",
        "Deleted",
        "Error",
    ]:
        return robot_account.get_state_display()

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
        missing_users = set(user.id for user in users) - set(
            user.id for user in resource_users
        )
        if missing_users:
            invalid_user_objects = [user for user in users if user.id in missing_users]
            user_details = []
            for user in invalid_user_objects:
                identifier = user.full_name or user.email
                user_details.append(str(identifier))
            raise serializers.ValidationError(
                f"Users {', '.join(user_details)} should belong to the same project or organization as the resource."
            )

        responsible_user = validated_data.get("responsible_user")
        if responsible_user and responsible_user not in resource_users:
            identifier = responsible_user.full_name or responsible_user.email
            raise serializers.ValidationError(
                f"The responsible user {identifier} should belong to the same project or organization as the resource."
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


class RobotAccountDetailsSerializer(
    core_serializers.RestrictedSerializerMixin, RobotAccountSerializer
):
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
    provider_uuid = serializers.UUIDField(
        read_only=True, source="resource.offering.customer.uuid"
    )
    provider_name = serializers.CharField(
        read_only=True, source="resource.offering.customer.name"
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
            "provider_uuid",
            "provider_name",
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
    total = serializers.IntegerField(read_only=True, help_text="Total revenue amount")
    year = serializers.IntegerField(
        read_only=True, source="invoice__year", help_text="Invoice year"
    )
    month = serializers.IntegerField(
        read_only=True, source="invoice__month", help_text="Invoice month"
    )


class AttributeSerializer(serializers.HyperlinkedModelSerializer):
    section_title = serializers.ReadOnlyField(source="section.title")

    class Meta:
        model = models.Attribute
        fields = (
            "url",
            "uuid",
            "key",
            "created",
            "title",
            "section",
            "section_title",
            "type",
            "required",
            "default",
        )
        extra_kwargs = dict(
            section={
                "lookup_field": "key",
                "view_name": "marketplace-section-detail",
            },
            url={
                "lookup_field": "uuid",
                "view_name": "marketplace-attribute-detail",
            },
        )
        read_only_fields = ["created"]


class AttributeOptionSerializer(serializers.HyperlinkedModelSerializer):
    attribute_title = serializers.ReadOnlyField(source="attribute.title")
    is_default = serializers.SerializerMethodField()

    class Meta:
        model = models.AttributeOption
        fields = (
            "url",
            "uuid",
            "id",
            "key",
            "title",
            "attribute",
            "attribute_title",
            "is_default",
        )
        extra_kwargs = dict(
            attribute={
                "lookup_field": "uuid",
                "view_name": "marketplace-attribute-detail",
            },
            url={
                "lookup_field": "uuid",
                "view_name": "marketplace-attribute-option-detail",
            },
        )

    def get_is_default(self, obj) -> bool:
        """Return True if this option is the default for its attribute."""
        return obj.attribute.default == obj.key

    def validate_attribute(self, value):
        if value.type != "choice":
            raise serializers.ValidationError(
                _("Options can only be added to attributes of type choice.")
            )
        return value

    def validate(self, data):
        return super().validate(data)


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
    agent_type = serializers.SerializerMethodField()

    class Meta:
        model = models.IntegrationStatus
        fields = (
            "agent_type",
            "status",
            "last_request_timestamp",
            "service_name",
        )

    def get_agent_type(
        self, integration_status: models.IntegrationStatus
    ) -> Literal[
        "Order processing",
        "Usage reporting",
        "Glauth sync",
        "Resource sync",
        "Event processing",
        "unknown",
    ]:
        agent_type_map = {k: v for k, v in models.IntegrationStatus.AgentTypes.CHOICES}
        return agent_type_map.get(int(integration_status.agent_type), "unknown")


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
    """Add an integration status field to the serializer."""
    fields["integration_status"] = serializers.SerializerMethodField()
    setattr(sender, "get_integration_status", get_integration_status)


core_signals.pre_serializer_fields.connect(
    sender=ProviderOfferingDetailsSerializer,
    receiver=add_integration_status,
)


class PluginComponentSerializer(serializers.Serializer):
    type = serializers.CharField(help_text="Type identifier of the component")
    name = serializers.CharField(help_text="Display name of the component")
    measured_unit = serializers.CharField(
        help_text="Unit of measurement for the component"
    )
    billing_type = serializers.ChoiceField(
        choices=BillingTypes.CHOICES, help_text="Billing type for the component"
    )


class PluginOfferingTypeSerializer(serializers.Serializer):
    offering_type = serializers.CharField()
    components = PluginComponentSerializer(many=True)
    available_limits = serializers.ListField(child=serializers.CharField())


class ServiceProviderStatisticsSerializer(serializers.Serializer):
    active_campaigns = serializers.IntegerField(
        read_only=True, help_text="Number of active campaigns"
    )
    current_customers = serializers.IntegerField(
        read_only=True, help_text="Number of current customers"
    )
    customers_number_change = serializers.IntegerField(
        read_only=True, help_text="Change in number of customers"
    )
    active_resources = serializers.IntegerField(
        read_only=True, help_text="Number of active resources"
    )
    resources_number_change = serializers.IntegerField(
        read_only=True, help_text="Change in number of resources"
    )
    active_and_paused_offerings = serializers.IntegerField(
        read_only=True, help_text="Number of active and paused offerings"
    )
    unresolved_tickets = serializers.IntegerField(
        read_only=True, help_text="Number of unresolved support tickets"
    )
    pending_orders = serializers.IntegerField(
        read_only=True, help_text="Number of pending orders"
    )
    erred_resources = serializers.IntegerField(
        read_only=True, help_text="Number of resources in error state"
    )


class NameUUIDSerializer(serializers.Serializer):
    name = serializers.CharField(read_only=True, help_text="Name of the entity")
    uuid = serializers.UUIDField(read_only=True, help_text="UUID of the entity")


class DetailStateSerializer(serializers.Serializer):
    detail = serializers.CharField(read_only=True)
    state = serializers.CharField(read_only=True)


class RemoveOfferingComponentSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(help_text="UUID of the component to remove")


class SwitchBillingModeSerializer(serializers.Serializer):
    billing_mode = serializers.ChoiceField(
        choices=[
            ("monthly", "Monthly (Limit-based)"),
            ("prepaid", "Prepaid (One-time)"),
            ("usage", "Usage-based"),
        ],
        help_text="Switch all builtin components to monthly (LIMIT), prepaid (ONE_TIME + is_prepaid), or usage-based billing.",
    )


class RemoveSoftwareCatalogSerializer(serializers.Serializer):
    offering_catalog_uuid = serializers.UUIDField(
        help_text="UUID of the offering catalog to remove"
    )


class RuntimeStatesSerializer(serializers.Serializer):
    value = serializers.CharField(
        read_only=True, help_text="Value of the runtime state"
    )
    label = serializers.CharField(
        read_only=True, help_text="Human-readable label for the runtime state"
    )


class CustomerMemberCountSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True, help_text="UUID of the customer")
    name = serializers.CharField(read_only=True, help_text="Name of the customer")
    abbreviation = serializers.CharField(
        read_only=True, help_text="Abbreviation of the customer"
    )
    count = serializers.IntegerField(read_only=True, help_text="Number of members")
    has_resources = serializers.BooleanField(
        read_only=True, help_text="Whether the customer has resources"
    )


class SubresourceOfferingSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True, help_text="UUID of the offering")
    type = serializers.CharField(read_only=True, help_text="Type of the offering")


class ImportableResourceSerializer(serializers.Serializer):
    backend_id = serializers.CharField(help_text="Backend identifier of the resource")
    name = serializers.CharField(help_text="Name of the resource")
    type = serializers.CharField(help_text="Type of the resource")
    description = serializers.CharField(
        allow_blank=True, help_text="Description of the resource"
    )


class OfferingReferenceSerializer(serializers.Serializer):
    offering_name = serializers.CharField(
        read_only=True, help_text="Name of the offering"
    )
    offering_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the offering"
    )


class OfferingGroupsSerializer(serializers.Serializer):
    customer_name = serializers.CharField(read_only=True)
    customer_uuid = serializers.CharField(read_only=True)
    offerings = OfferingReferenceSerializer(many=True, read_only=True)


class BackendResourceSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.objects.all(), slug_field="uuid"
    )
    project_name = serializers.CharField(read_only=True, source="project.name")
    project_url = serializers.HyperlinkedRelatedField(
        source="project",
        lookup_field="uuid",
        view_name="project-detail",
        read_only=True,
    )

    offering = serializers.SlugRelatedField(
        queryset=models.Offering.objects.all(),
        slug_field="uuid",
    )
    offering_name = serializers.CharField(read_only=True, source="offering.name")
    offering_url = serializers.HyperlinkedRelatedField(
        source="offering",
        lookup_field="uuid",
        view_name="marketplace-public-offering-detail",
        read_only=True,
    )

    class Meta:
        model = models.BackendResource
        fields = (
            "url",
            "uuid",
            "name",
            "created",
            "modified",
            "project",
            "project_name",
            "project_url",
            "offering",
            "offering_name",
            "offering_url",
            "backend_id",
            "backend_metadata",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }
        view_name = "backend-resource-detail"


class BackendResourceImportSerializer(serializers.Serializer):
    plan = serializers.SlugRelatedField(
        queryset=models.Plan.objects.all(), slug_field="uuid", required=False
    )


# Using shortened "Request" name due to conflict resulting a `spectacular` error
class BackendResourceReqSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    offering = serializers.SlugRelatedField(
        queryset=models.Offering.objects.all(), slug_field="uuid", required=True
    )
    offering_name = serializers.CharField(read_only=True, source="offering.name")
    offering_url = serializers.HyperlinkedRelatedField(
        source="offering",
        lookup_field="uuid",
        view_name="marketplace-public-offering-detail",
        read_only=True,
    )

    class Meta:
        model = models.BackendResourceRequest
        fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "started",
            "finished",
            "state",
            "offering",
            "offering_name",
            "offering_url",
            "error_message",
            "error_traceback",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
            },
        }
        view_name = "backend-resource-request-detail"
        read_only_fields = (
            "uuid",
            "state",
            "started",
            "finished",
            "error_message",
            "error_traceback",
        )


class BackendResourceRequestSetErredSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = models.BackendResourceRequest
        fields = ("error_message", "error_traceback")
        protected_fields = ("error_message", "error_traceback")


class MaintenanceAnnouncementOfferingSerializer(serializers.HyperlinkedModelSerializer):
    impact_level_display = serializers.SerializerMethodField()
    offering_name = serializers.CharField(read_only=True, source="offering.name")

    def get_impact_level_display(
        self, obj: models.MaintenanceAnnouncement
    ) -> Literal[
        "No impact",
        "Degraded performance",
        "Partial outage",
        "Full outage",
    ]:
        return obj.get_impact_level_display()

    class Meta:
        model = models.MaintenanceAnnouncementOffering
        fields = [
            "url",
            "uuid",
            "maintenance",
            "offering",
            "impact_level",
            "impact_level_display",
            "impact_description",
            "offering_name",
        ]
        extra_kwargs = {
            "url": {
                "view_name": "maintenance-announcement-offering-detail",
                "lookup_field": "uuid",
            },
            "maintenance": {
                "view_name": "maintenance-announcement-detail",
                "lookup_field": "uuid",
            },
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        }

    def validate_maintenance(self, value):
        user = self.context["request"].user
        if not (user.is_staff or value.service_provider.customer.has_user(user)):
            raise serializers.ValidationError(
                "You are not related to this service provider's customer."
            )
        return value

    def validate_offering(self, value):
        if not value.shared:
            raise serializers.ValidationError(
                "Only shared offerings can be included in maintenance announcements."
            )
        return value


class MaintenanceAnnouncementSerializer(serializers.HyperlinkedModelSerializer):
    affected_offerings = MaintenanceAnnouncementOfferingSerializer(
        source="affected_offerings.all",
        many=True,
        read_only=True,
    )
    service_provider_name = serializers.CharField(
        read_only=True, source="service_provider.customer.name"
    )
    state = serializers.SerializerMethodField()

    def get_state(
        self, obj: models.MaintenanceAnnouncement
    ) -> Literal[
        "Draft",
        "Scheduled",
        "In progress",
        "Completed",
        "Cancelled",
    ]:
        return obj.get_state_display()

    class Meta:
        model = models.MaintenanceAnnouncement
        fields = [
            "url",
            "uuid",
            "name",
            "message",
            "internal_notes",
            "maintenance_type",
            "external_reference_url",
            "state",
            "scheduled_start",
            "scheduled_end",
            "actual_start",
            "actual_end",
            "service_provider",
            "created_by",
            "affected_offerings",
            "service_provider_name",
            "state",
            "backend_id",
        ]
        read_only_fields = (
            "state",
            "actual_start",
            "actual_end",
            "created_by",
            "backend_id",
        )
        extra_kwargs = {
            "service_provider": {
                "lookup_field": "uuid",
                "view_name": "marketplace-service-provider-detail",
            },
            "url": {
                "view_name": "maintenance-announcement-detail",
                "lookup_field": "uuid",
            },
            "created_by": {
                "view_name": "user-detail",
                "lookup_field": "uuid",
                "read_only": True,
            },
        }

    def validate_service_provider(self, value):
        user = self.context["request"].user
        if not (user.is_staff or value.customer.has_user(user)):
            raise serializers.ValidationError(
                "You are not related to this service provider's customer."
            )
        return value

    def create(self, validated_data):
        request = self.context["request"]
        validated_data["created_by"] = request.user
        return super().create(validated_data)


class PublicMaintenanceAnnouncementSerializer(serializers.HyperlinkedModelSerializer):
    affected_offerings = MaintenanceAnnouncementOfferingSerializer(
        source="affected_offerings.all",
        many=True,
        read_only=True,
    )
    service_provider_name = serializers.CharField(
        read_only=True, source="service_provider.customer.name"
    )
    state = serializers.SerializerMethodField()
    maintenance_type_display = serializers.CharField(
        read_only=True, source="get_maintenance_type_display"
    )

    def get_state(
        self, obj: models.MaintenanceAnnouncement
    ) -> Literal[
        "Scheduled",
        "In progress",
        "Completed",
    ]:
        return obj.get_state_display()

    class Meta:
        model = models.MaintenanceAnnouncement
        fields = [
            "url",
            "uuid",
            "name",
            "message",
            "maintenance_type",
            "maintenance_type_display",
            "external_reference_url",
            "state",
            "scheduled_start",
            "scheduled_end",
            "actual_start",
            "actual_end",
            "affected_offerings",
            "service_provider_name",
        ]
        read_only_fields = fields
        extra_kwargs = {
            "url": {
                "view_name": "public-maintenance-announcement-detail",
                "lookup_field": "uuid",
            },
        }


class MaintenanceAnnouncementOfferingTemplateSerializer(
    MaintenanceAnnouncementOfferingSerializer
):
    offering_name = serializers.CharField(read_only=True, source="offering.name")
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")

    class Meta(MaintenanceAnnouncementOfferingSerializer.Meta):
        model = models.MaintenanceAnnouncementOfferingTemplate
        fields = [
            "url",
            "uuid",
            "maintenance_template",
            "offering",
            "offering_name",
            "offering_uuid",
            "impact_level",
            "impact_description",
        ]

        extra_kwargs = {
            "url": {
                "view_name": "maintenance-announcement-template-offering-detail",
                "lookup_field": "uuid",
            },
            "maintenance_template": {
                "view_name": "maintenance-announcement-template-detail",
                "lookup_field": "uuid",
            },
            "offering": {
                "lookup_field": "uuid",
                "view_name": "marketplace-provider-offering-detail",
            },
        }

    def validate_maintenance_template(self, value):
        user = self.context["request"].user
        if not (user.is_staff or value.service_provider.customer.has_user(user)):
            raise serializers.ValidationError(
                "You are not related to this service provider's customer."
            )
        return value


class MaintenanceAnnouncementTemplateSerializer(MaintenanceAnnouncementSerializer):
    class Meta(MaintenanceAnnouncementSerializer.Meta):
        model = models.MaintenanceAnnouncementTemplate
        fields = [
            "url",
            "uuid",
            "name",
            "message",
            "maintenance_type",
            "service_provider",
            "affected_offerings",
        ]
        extra_kwargs = {
            "service_provider": {
                "lookup_field": "uuid",
                "view_name": "marketplace-service-provider-detail",
            },
            "url": {
                "view_name": "maintenance-announcement-template-detail",
                "lookup_field": "uuid",
            },
        }

    def create(self, validated_data):
        return serializers.HyperlinkedModelSerializer.create(self, validated_data)


class MaintenanceActionResponseSerializer(serializers.Serializer):
    """Serializer for maintenance action responses."""

    detail = serializers.CharField(
        help_text="Response message describing the action result"
    )


class UserOfferingConsentSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    offering_slug = serializers.ReadOnlyField(source="offering.slug")
    offering_url = serializers.HyperlinkedRelatedField(
        source="offering",
        lookup_field="uuid",
        view_name="marketplace-provider-offering-detail",
        read_only=True,
    )
    user_username = serializers.ReadOnlyField(source="user.username")
    user_full_name = serializers.ReadOnlyField(source="user.full_name")
    user_email = serializers.ReadOnlyField(source="user.email")

    has_consent = serializers.SerializerMethodField()
    requires_reconsent = serializers.SerializerMethodField()
    collected_attributes = serializers.SerializerMethodField(
        help_text="List of user attributes that will be shared with service provider"
    )

    class Meta:
        model = models.UserOfferingConsent
        fields = (
            "uuid",
            "user_uuid",
            "offering_uuid",
            "agreement_date",
            "version",
            "revocation_date",
            "created",
            "user_username",
            "user_full_name",
            "user_email",
            "offering_name",
            "offering_slug",
            "offering_url",
            "modified",
            "has_consent",
            "requires_reconsent",
            "collected_attributes",
        )
        read_only_fields = ("agreement_date", "revocation_date", "created", "modified")

    def get_collected_attributes(self, obj) -> list[str]:
        """Return list of user attributes that will be collected for this offering."""
        return models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(
            obj.offering
        )

    def get_has_consent(self, obj) -> bool:
        return obj.revocation_date is None

    def get_requires_reconsent(self, obj) -> bool:
        if obj.revocation_date is not None:
            return False

        active_tos = obj.offering.terms_of_service_configs.filter(
            is_active=True
        ).first()
        if not active_tos or not active_tos.requires_reconsent:
            return False
        return active_tos.version != obj.version


class UserConsentInfoSerializer(serializers.Serializer):
    """Serializer for user consent information in Terms of Service responses."""

    uuid = serializers.UUIDField(read_only=True)
    version = serializers.CharField(read_only=True)
    agreement_date = serializers.DateTimeField(read_only=True)
    is_revoked = serializers.BooleanField(read_only=True)


class UserOfferingConsentCreateSerializer(serializers.Serializer):
    offering = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.Offering.objects.all(),
        required=True,
    )

    def validate(self, attrs):
        offering = attrs["offering"]
        request = self.context.get("request")
        user = request.user if request else None

        if not offering.has_terms_of_service():
            raise serializers.ValidationError(
                "This offering does not have Terms of Service."
            )

        active_tos = offering.terms_of_service_configs.filter(is_active=True).first()
        if not active_tos:
            raise serializers.ValidationError(
                "This offering does not have active Terms of Service."
            )

        if user:
            existing_consent = models.UserOfferingConsent.objects.filter(
                user=user,
                offering=offering,
                revocation_date__isnull=True,
            ).first()

            if existing_consent:
                if existing_consent.version == active_tos.version:
                    raise serializers.ValidationError(
                        "You have already consented to the current Terms of Service for this offering."
                    )

        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        user = request.user
        offering = validated_data["offering"]

        active_tos = offering.terms_of_service_configs.filter(is_active=True).first()

        consent, created = models.UserOfferingConsent.objects.get_or_create(
            user=user,
            offering=offering,
            defaults={
                "version": active_tos.version or "",
            },
        )

        # If consent already existed (even if revoked), update it
        if not created:
            consent.version = active_tos.version or ""
            consent.revocation_date = None
            consent.save()

        return consent


class OfferingTermsOfServiceSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.ModelSerializer,
):
    """Serializer for Terms of Service configurations."""

    offering_uuid = serializers.UUIDField(read_only=True, source="offering.uuid")
    offering_name = serializers.CharField(read_only=True, source="offering.name")
    user_consent = serializers.SerializerMethodField()
    has_user_consent = serializers.SerializerMethodField()

    class Meta:
        model = models.OfferingTermsOfService
        fields = (
            "uuid",
            "offering_uuid",
            "offering_name",
            "terms_of_service",
            "terms_of_service_link",
            "version",
            "is_active",
            "requires_reconsent",
            "grace_period_days",
            "user_consent",
            "has_user_consent",
            "created",
            "modified",
        )
        read_only_fields = ("created", "modified")
        protected_fields = (
            "version",
            "requires_reconsent",
        )

    def validate(self, attrs):
        if attrs.get("is_active", False):
            offering = self.instance.offering if self.instance else attrs["offering"]
            existing_active = models.OfferingTermsOfService.objects.filter(
                offering=offering, is_active=True
            ).exists()
            if existing_active:
                raise serializers.ValidationError(
                    "An active Terms of Service configuration already exists for this offering. "
                    "Please deactivate the existing configuration before creating a new active one."
                )
        return attrs

    @extend_schema_field(UserConsentInfoSerializer(allow_null=True))
    def get_user_consent(self, obj):
        request = self.context.get("request")
        if not request or not request.user:
            return None
        user = request.user
        offering = obj.offering
        consent = models.UserOfferingConsent.objects.filter(
            user=user, offering=offering, revocation_date__isnull=True
        ).first()
        if not consent:
            return None
        return {
            "uuid": consent.uuid,
            "version": consent.version,
            "agreement_date": consent.agreement_date,
            "is_revoked": consent.is_revoked,
        }

    @extend_schema_field(serializers.BooleanField())
    def get_has_user_consent(self, obj):
        """
        Check if user has valid consent for this specific ToS version.

        If requires_reconsent=True, only returns True if consent version matches
        this ToS version. Otherwise, returns True if any active consent exists.
        """
        request = self.context.get("request")
        if not request or not request.user:
            return False

        user = request.user
        offering = obj.offering

        consent = models.UserOfferingConsent.objects.filter(
            user=user, offering=offering, revocation_date__isnull=True
        ).first()

        if not consent:
            return False

        if obj.requires_reconsent:
            return consent.version == obj.version

        return True


class OfferingTermsOfServiceCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating Terms of Service configurations."""

    offering = serializers.HyperlinkedRelatedField(
        queryset=models.Offering.objects.all(),
        view_name="marketplace-provider-offering-detail",
        lookup_field="uuid",
        required=True,
    )

    class Meta:
        model = models.OfferingTermsOfService
        fields = (
            "offering",
            "terms_of_service",
            "terms_of_service_link",
            "version",
            "is_active",
            "requires_reconsent",
            "grace_period_days",
        )

    def validate(self, attrs):
        offering = attrs["offering"]
        request = self.context.get("request")

        if request:
            has_offering_permission = has_permission(
                request, PermissionEnum.UPDATE_OFFERING, offering
            )
            has_customer_permission = has_permission(
                request, PermissionEnum.UPDATE_OFFERING, offering.customer
            )
            has_service_provider_permission = has_permission(
                request,
                PermissionEnum.UPDATE_OFFERING,
                offering.customer.serviceprovider,
            )

            if not any(
                [
                    has_offering_permission,
                    has_customer_permission,
                    has_service_provider_permission,
                ]
            ):
                raise PermissionDenied(
                    "You don't have permission to manage Terms of Service for this offering."
                )

        if attrs.get("is_active", False):
            existing_active = models.OfferingTermsOfService.objects.filter(
                offering=offering, is_active=True
            ).exists()
            if existing_active:
                raise serializers.ValidationError(
                    "An active Terms of Service configuration already exists for this offering. "
                    "Please deactivate the existing configuration before creating a new active one."
                )

        return attrs


class OfferingTermsOfServiceUpdateSerializer(serializers.Serializer):
    """Serializer for updating Terms of Service for an offering."""

    terms_of_service = core_serializers.HTMLCleanField(required=False, allow_blank=True)
    terms_of_service_link = serializers.URLField(required=False, allow_blank=True)
    version = serializers.CharField(max_length=50, required=False, allow_blank=True)
    requires_reconsent = serializers.BooleanField(required=False, default=False)


class OfferingUserAttributeConfigSerializer(serializers.ModelSerializer):
    """
    Serializer for configuring which user attributes an offering exposes.
    Supports GDPR compliance by declaring personal data processing.

    Used as a nested action under ProviderOfferingViewSet.
    """

    offering = serializers.SlugRelatedField(
        queryset=models.Offering.objects.all(),
        slug_field="uuid",
        write_only=True,
        required=False,
    )
    offering_uuid = serializers.ReadOnlyField(source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")

    exposed_fields = serializers.SerializerMethodField()
    is_default = serializers.SerializerMethodField()

    class Meta:
        model = models.OfferingUserAttributeConfig
        fields = (
            "uuid",
            "created",
            "modified",
            "offering",
            "offering_uuid",
            "offering_name",
            # Core attributes
            "expose_username",
            "expose_full_name",
            "expose_email",
            # Extended profile
            "expose_phone_number",
            "expose_organization",
            "expose_job_title",
            "expose_affiliations",
            # User profile attributes
            "expose_gender",
            "expose_personal_title",
            "expose_place_of_birth",
            "expose_country_of_residence",
            "expose_nationality",
            "expose_nationalities",
            "expose_organization_country",
            "expose_organization_type",
            "expose_organization_registry_code",
            "expose_eduperson_assurance",
            # Legal and identity attributes
            "expose_civil_number",
            "expose_birth_date",
            "expose_identity_source",
            # Identity Bridge attributes
            "expose_active_isds",
            # Computed
            "exposed_fields",
            "is_default",
        )
        read_only_fields = (
            "uuid",
            "created",
            "modified",
            "offering_uuid",
            "offering_name",
        )

    def get_exposed_fields(self, obj) -> list[str]:
        """Return list of field names currently configured for exposure."""
        return obj.get_exposed_fields()

    def get_is_default(self, obj) -> bool:
        """Return True if this is a default (unsaved) config."""
        return obj.pk is None


class CourseAccountSerializer(serializers.HyperlinkedModelSerializer):
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.available_objects.filter(
            kind=ProjectKind.COURSE,
        ),
        slug_field="uuid",
    )
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.CharField(read_only=True, source="project.name")
    project_slug = serializers.CharField(read_only=True, source="project.slug")
    project_start_date = serializers.DateField(
        read_only=True, source="project.start_date"
    )
    project_end_date = serializers.DateField(read_only=True, source="project.end_date")

    user_uuid = serializers.UUIDField(read_only=True, source="user.uuid")
    username = serializers.CharField(read_only=True, source="user.username")

    customer_uuid = serializers.UUIDField(
        read_only=True, source="project.customer.uuid"
    )
    customer_name = serializers.CharField(
        read_only=True, source="project.customer.name"
    )

    state = serializers.SerializerMethodField()

    class Meta:
        model = models.CourseAccount
        fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "project",
            "project_uuid",
            "project_name",
            "project_slug",
            "project_start_date",
            "project_end_date",
            "user_uuid",
            "username",
            "customer_uuid",
            "customer_name",
            "state",
            "email",
            "description",
            "error_message",
            "error_traceback",
        )
        read_only_fields = [
            "error_message",
            "error_traceback",
        ]
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-course-account-detail",
            },
        }

    @extend_schema_field(serializers.ChoiceField(choices=CourseAccountState.labels))
    def get_state(self, course_account: models.CourseAccount) -> str:
        return course_account.get_state_display()

    def validate(self, attrs):
        super().validate(attrs)
        project: structure_models.Project = attrs["project"]
        if project.end_date is None:
            message = f"Unable to create a course account for a course project {project} without an end_date"
            logger.error(message)
            raise ValidationError(message)

        return attrs


class CourseAccountCreateNestedSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.CourseAccount
        fields = (
            "email",
            "description",
        )


class CourseAccountsBulkCreateSerializer(serializers.Serializer):
    course_accounts = CourseAccountCreateNestedSerializer(many=True)
    project = serializers.SlugRelatedField(
        queryset=structure_models.Project.available_objects.filter(
            kind=ProjectKind.COURSE,
        ),
        slug_field="uuid",
    )

    def validate(self, attrs):
        super().validate(attrs)

        # Validate that course_accounts list is not empty
        if not attrs.get("course_accounts"):
            raise ValidationError({"course_accounts": "This field cannot be empty."})

        project: structure_models.Project = attrs["project"]
        if project.end_date is None:
            message = f"Unable to create a course account for a course project {project} without an end_date"
            logger.error(message)
            raise ValidationError(message)

        return attrs


class VersionAdoptionSerializer(serializers.Serializer):
    """Serializer for version adoption statistics."""

    version = serializers.CharField(read_only=True, help_text="Version identifier")
    users_count = serializers.IntegerField(
        read_only=True, help_text="Number of users on this version"
    )


class TimeSeriesToSDataSerializer(serializers.Serializer):
    date = serializers.DateField(read_only=True, help_text="Date of the data point")
    count = serializers.IntegerField(read_only=True, help_text="Count for the date")


class ToSConsentDashboardSerializer(serializers.Serializer):
    """Serializer for Terms of Service consent dashboard statistics."""

    active_users_count = serializers.IntegerField(
        read_only=True, help_text="Number of active users"
    )
    total_users_count = serializers.IntegerField(
        read_only=True, help_text="Total number of users"
    )
    active_users_percentage = serializers.FloatField(
        read_only=True, help_text="Percentage of active users"
    )

    accepted_consents_count = serializers.IntegerField(
        read_only=True, help_text="Number of accepted consents"
    )
    revoked_consents_count = serializers.IntegerField(
        read_only=True, help_text="Number of revoked consents"
    )
    total_consents_count = serializers.IntegerField(
        read_only=True, help_text="Total number of consents"
    )

    revoked_consents_over_time = serializers.ListField(
        child=TimeSeriesToSDataSerializer(), read_only=True
    )

    tos_version_adoption = serializers.ListField(
        child=VersionAdoptionSerializer(), read_only=True
    )
    active_users_over_time = serializers.ListField(
        child=TimeSeriesToSDataSerializer(), read_only=True
    )
    accepted_consents_over_time = serializers.ListField(
        child=TimeSeriesToSDataSerializer(), read_only=True
    )


class SoftwareCatalogSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for unified SoftwareCatalog model."""

    package_count = serializers.SerializerMethodField()
    version_count = serializers.SerializerMethodField()
    target_count = serializers.SerializerMethodField()
    catalog_type_display = serializers.CharField(
        source="get_catalog_type_display", read_only=True
    )

    class Meta:
        model = models.SoftwareCatalog
        fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "name",
            "version",
            "catalog_type",
            "catalog_type_display",
            "source_url",
            "description",
            "metadata",
            "auto_update_enabled",
            "last_update_attempt",
            "last_successful_update",
            "update_errors",
            "package_count",
            "version_count",
            "target_count",
        )
        read_only_fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "catalog_type_display",
            "last_update_attempt",
            "last_successful_update",
            "package_count",
            "version_count",
            "target_count",
        )
        extra_kwargs = {
            "url": {
                "view_name": "marketplace-software-catalog-detail",
                "lookup_field": "uuid",
            }
        }

    @extend_schema_field(serializers.IntegerField())
    def get_package_count(self, obj):
        """Get total number of packages in this catalog."""
        return obj.packages.count()

    @extend_schema_field(serializers.IntegerField())
    def get_version_count(self, obj):
        """Get total number of versions across all packages in this catalog."""
        return models.SoftwareVersion.objects.filter(package__catalog=obj).count()

    @extend_schema_field(serializers.IntegerField())
    def get_target_count(self, obj):
        """Get total number of targets across all versions in this catalog."""
        return models.SoftwareTarget.objects.filter(
            version__package__catalog=obj
        ).count()


class SoftwareCatalogImportSerializer(serializers.Serializer):
    """Input serializer for the import_catalog action."""

    name = serializers.ChoiceField(choices=["EESSI", "Spack"])


class SoftwareCatalogDiscoverSerializer(serializers.Serializer):
    """Read-only serializer for the discover action response schema."""

    name = serializers.CharField()
    catalog_type = serializers.CharField()
    latest_version = serializers.CharField()
    existing = serializers.BooleanField()
    existing_version = serializers.CharField(allow_null=True)
    update_available = serializers.BooleanField()


class NestedSoftwareTargetSerializer(serializers.ModelSerializer):
    """Nested serializer for unified SoftwareTarget model."""

    class Meta:
        model = models.SoftwareTarget
        fields = (
            "uuid",
            "target_type",
            "target_name",
            "target_subtype",
            "location",
            "metadata",
            "gpu_architectures",
        )


class NestedSoftwareVersionSerializer(serializers.ModelSerializer):
    """Nested serializer for SoftwareVersion model."""

    targets = NestedSoftwareTargetSerializer(many=True, read_only=True)

    # Expose key EESSI fields at top level for convenience
    module = serializers.SerializerMethodField()
    required_modules = serializers.SerializerMethodField()
    extensions = serializers.SerializerMethodField()
    toolchain = serializers.SerializerMethodField()
    toolchain_families_compatibility = serializers.SerializerMethodField()

    class Meta:
        model = models.SoftwareVersion
        fields = (
            "uuid",
            "version",
            "release_date",
            "targets",
            "module",
            "required_modules",
            "extensions",
            "toolchain",
            "toolchain_families_compatibility",
        )

    @extend_schema_field(serializers.DictField())
    def get_module(self, obj):
        """Return structured module info."""
        return obj.metadata.get("module", {})

    @extend_schema_field(serializers.ListField())
    def get_required_modules(self, obj):
        """Return structured required_modules list."""
        return obj.metadata.get("required_modules", [])

    @extend_schema_field(serializers.ListField())
    def get_extensions(self, obj):
        """Return extensions bundled with this version."""
        return obj.metadata.get("extensions", [])

    @extend_schema_field(serializers.DictField())
    def get_toolchain(self, obj):
        """Return toolchain info."""
        return obj.metadata.get("toolchain", {})

    @extend_schema_field(serializers.ListField())
    def get_toolchain_families_compatibility(self, obj):
        """Return toolchain compatibility list."""
        return obj.metadata.get("toolchain_families_compatibility", [])


class NestedParentSoftwareSerializer(serializers.HyperlinkedModelSerializer):
    versions = serializers.SerializerMethodField()

    class Meta:
        model = models.SoftwarePackage
        fields = ("uuid", "name", "url", "versions")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "marketplace-software-package-detail",
            },
        }

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_versions(self, obj):
        return list(obj.versions.values_list("version", flat=True))


class SoftwarePackageSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for unified SoftwarePackage model."""

    catalog_name = serializers.CharField(source="catalog.name", read_only=True)
    catalog_version = serializers.CharField(source="catalog.version", read_only=True)
    catalog_type = serializers.CharField(source="catalog.catalog_type", read_only=True)
    catalog_type_display = serializers.CharField(
        source="catalog.get_catalog_type_display", read_only=True
    )
    version_count = serializers.SerializerMethodField()
    extension_count = serializers.SerializerMethodField()
    versions = NestedSoftwareVersionSerializer(many=True, read_only=True)
    parent_softwares = NestedParentSoftwareSerializer(many=True, read_only=True)
    extensions = NestedParentSoftwareSerializer(many=True, read_only=True)

    class Meta:
        model = models.SoftwarePackage
        fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "catalog",
            "name",
            "description",
            "homepage",
            "categories",
            "licenses",
            "maintainers",
            "is_extension",
            "parent_softwares",
            "extensions",
            "catalog_name",
            "catalog_version",
            "catalog_type",
            "catalog_type_display",
            "version_count",
            "extension_count",
            "versions",
        )
        read_only_fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "catalog_name",
            "catalog_version",
            "catalog_type",
            "catalog_type_display",
            "version_count",
            "extension_count",
            "versions",
        )
        extra_kwargs = {
            "url": {
                "view_name": "marketplace-software-package-detail",
                "lookup_field": "uuid",
            },
            "catalog": {
                "view_name": "marketplace-software-catalog-detail",
                "lookup_field": "uuid",
            },
        }

    @extend_schema_field(serializers.IntegerField())
    def get_version_count(self, obj):
        """Get number of versions for this package."""
        return obj.versions.count()

    @extend_schema_field(serializers.IntegerField())
    def get_extension_count(self, obj):
        """Get number of extension packages for this package."""
        return obj.extension_count


class SoftwareVersionSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for unified SoftwareVersion model."""

    package_name = serializers.CharField(source="package.name", read_only=True)
    catalog_type = serializers.CharField(
        source="package.catalog.catalog_type", read_only=True
    )
    target_count = serializers.SerializerMethodField()

    # Expose key EESSI fields at top level for convenience
    module = serializers.SerializerMethodField()
    required_modules = serializers.SerializerMethodField()
    extensions = serializers.SerializerMethodField()
    toolchain = serializers.SerializerMethodField()
    toolchain_families_compatibility = serializers.SerializerMethodField()

    class Meta:
        model = models.SoftwareVersion
        fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "version",
            "release_date",
            "dependencies",
            "metadata",
            "package_name",
            "catalog_type",
            "target_count",
            "module",
            "required_modules",
            "extensions",
            "toolchain",
            "toolchain_families_compatibility",
        )
        read_only_fields = fields
        extra_kwargs = {
            "url": {
                "view_name": "marketplace-software-version-detail",
                "lookup_field": "uuid",
            }
        }

    @extend_schema_field(serializers.IntegerField())
    def get_target_count(self, obj):
        """Get number of targets for this version."""
        return obj.targets.count()

    @extend_schema_field(serializers.DictField())
    def get_module(self, obj):
        """Return structured module info."""
        return obj.metadata.get("module", {})

    @extend_schema_field(serializers.ListField())
    def get_required_modules(self, obj):
        """Return structured required_modules list."""
        return obj.metadata.get("required_modules", [])

    @extend_schema_field(serializers.ListField())
    def get_extensions(self, obj):
        """Return extensions bundled with this version."""
        return obj.metadata.get("extensions", [])

    @extend_schema_field(serializers.DictField())
    def get_toolchain(self, obj):
        """Return toolchain info."""
        return obj.metadata.get("toolchain", {})

    @extend_schema_field(serializers.ListField())
    def get_toolchain_families_compatibility(self, obj):
        """Return toolchain compatibility list."""
        return obj.metadata.get("toolchain_families_compatibility", [])


class SoftwareTargetSerializer(serializers.HyperlinkedModelSerializer):
    """Serializer for unified SoftwareTarget model."""

    class Meta:
        model = models.SoftwareTarget
        fields = (
            "url",
            "uuid",
            "created",
            "modified",
            "target_type",
            "target_name",
            "target_subtype",
            "location",
            "metadata",
            "gpu_architectures",
        )
        read_only_fields = fields
        extra_kwargs = {
            "url": {
                "view_name": "marketplace-software-target-detail",
                "lookup_field": "uuid",
            }
        }


class OfferingSoftwareCatalogSerializer(serializers.ModelSerializer):
    """Serializer for OfferingSoftwareCatalog model."""

    offering_name = serializers.CharField(source="offering.name", read_only=True)
    catalog_name = serializers.CharField(source="catalog.name", read_only=True)
    catalog_version = serializers.CharField(source="catalog.version", read_only=True)
    offering = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.Offering.objects.all()
    )
    catalog = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.SoftwareCatalog.objects.all()
    )
    partition = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.OfferingPartition.objects.all(),
        required=False,
        allow_null=True,
    )
    partition_name = serializers.CharField(
        source="partition.partition_name", read_only=True
    )

    class Meta:
        model = models.OfferingSoftwareCatalog
        fields = (
            "uuid",
            "created",
            "modified",
            "offering",
            "catalog",
            "offering_name",
            "catalog_name",
            "catalog_version",
            "enabled_cpu_family",
            "enabled_cpu_microarchitectures",
            "partition",
            "partition_name",
        )


class OfferingSoftwareCatalogUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating OfferingSoftwareCatalog model."""

    offering_catalog_uuid = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.OfferingSoftwareCatalog.objects.all(),
        write_only=True,
    )
    catalog = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.SoftwareCatalog.objects.all()
    )
    partition = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.OfferingPartition.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.OfferingSoftwareCatalog
        fields = (
            "offering_catalog_uuid",
            "catalog",
            "enabled_cpu_family",
            "enabled_cpu_microarchitectures",
            "partition",
        )


class OfferingPartitionUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating OfferingPartition model."""

    partition_uuid = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.OfferingPartition.objects.all(),
        write_only=True,
    )

    class Meta:
        model = models.OfferingPartition
        fields = (
            "partition_uuid",
            "partition_name",
            "cpu_arch",
            "gpu_arch",
            "cpu_bind",
            "def_cpu_per_gpu",
            "max_cpus_per_node",
            "max_cpus_per_socket",
            "def_mem_per_cpu",
            "def_mem_per_gpu",
            "def_mem_per_node",
            "max_mem_per_cpu",
            "max_mem_per_node",
            "default_time",
            "max_time",
            "grace_time",
            "max_nodes",
            "min_nodes",
            "exclusive_topo",
            "exclusive_user",
            "priority_tier",
            "qos",
            "req_resv",
        )


class OfferingPartitionSerializer(serializers.ModelSerializer):
    """Serializer for OfferingPartition model."""

    offering_name = serializers.CharField(source="offering.name", read_only=True)
    offering = serializers.SlugRelatedField(
        slug_field="uuid", queryset=models.Offering.objects.all()
    )

    class Meta:
        model = models.OfferingPartition
        fields = (
            "uuid",
            "created",
            "modified",
            "offering",
            "offering_name",
            "partition_name",
            "cpu_arch",
            "gpu_arch",
            "cpu_bind",
            "def_cpu_per_gpu",
            "max_cpus_per_node",
            "max_cpus_per_socket",
            "def_mem_per_cpu",
            "def_mem_per_gpu",
            "def_mem_per_node",
            "max_mem_per_cpu",
            "max_mem_per_node",
            "default_time",
            "max_time",
            "grace_time",
            "max_nodes",
            "min_nodes",
            "exclusive_topo",
            "exclusive_user",
            "priority_tier",
            "qos",
            "req_resv",
        )
        read_only_fields = ("uuid", "created", "modified")
        extra_kwargs = {
            "url": {
                "view_name": "marketplace-offering-partition-detail",
                "lookup_field": "uuid",
            }
        }


class RemovePartitionSerializer(serializers.Serializer):
    partition_uuid = serializers.UUIDField()


class OfferingExportParametersSerializer(serializers.Serializer):
    """
    Serializer to configure offering export parameters.

    Controls which attributes and related entities to include in the export.
    """

    include_components = serializers.BooleanField(
        default=True, help_text="Include offering components in export"
    )
    include_plans = serializers.BooleanField(
        default=True, help_text="Include offering plans in export"
    )
    include_screenshots = serializers.BooleanField(
        default=True, help_text="Include offering screenshots in export"
    )
    include_files = serializers.BooleanField(
        default=True, help_text="Include offering files in export"
    )
    include_endpoints = serializers.BooleanField(
        default=True, help_text="Include offering access endpoints in export"
    )
    include_organization_groups = serializers.BooleanField(
        default=True, help_text="Include organization groups associations in export"
    )
    include_terms_of_service = serializers.BooleanField(
        default=True, help_text="Include terms of service configurations in export"
    )
    include_plugin_options = serializers.BooleanField(
        default=True, help_text="Include plugin options in export"
    )
    include_secret_options = serializers.BooleanField(
        default=False,
        help_text="Include secret options in export (WARNING: sensitive data)",
    )
    include_attributes = serializers.BooleanField(
        default=True, help_text="Include offering attributes in export"
    )
    include_options = serializers.BooleanField(
        default=True, help_text="Include offering options in export"
    )
    include_resource_options = serializers.BooleanField(
        default=True, help_text="Include resource options in export"
    )


class ExportOfferingDataSerializer(serializers.Serializer):
    """Serializer for core offering data in export."""

    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    full_description = serializers.CharField(allow_blank=True)
    vendor_details = serializers.CharField(allow_blank=True)
    getting_started = serializers.CharField(allow_blank=True)
    integration_guide = serializers.CharField(allow_blank=True)
    type = serializers.CharField()
    shared = serializers.BooleanField()
    billable = serializers.BooleanField()
    state = serializers.CharField()
    category_name = serializers.CharField(allow_null=True)
    country = serializers.CharField(allow_blank=True)
    latitude = serializers.FloatField(allow_null=True)
    longitude = serializers.FloatField(allow_null=True)
    access_url = serializers.URLField(allow_blank=True)
    paused_reason = serializers.CharField(allow_blank=True)
    attributes = serializers.JSONField(required=False)
    options = serializers.JSONField(required=False)


class ExportComponentDataSerializer(serializers.Serializer):
    """Serializer for component data in export."""

    type = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    billing_type = serializers.CharField()
    measured_unit = serializers.CharField()
    unit_factor = serializers.FloatField(allow_null=True)
    limit_period = serializers.CharField(allow_null=True)
    limit_amount = serializers.IntegerField(allow_null=True)
    article_code = serializers.CharField(allow_blank=True)
    backend_id = serializers.CharField(allow_blank=True)


class ExportPlanComponentDataSerializer(serializers.Serializer):
    """Serializer for plan component data in export."""

    component_type = serializers.CharField(allow_null=True)
    amount = serializers.IntegerField()
    price = serializers.FloatField()
    future_price = serializers.FloatField(allow_null=True)


class ExportPlanDataSerializer(serializers.Serializer):
    """Serializer for plan data in export."""

    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    unit_price = serializers.FloatField()
    unit = serializers.CharField()
    archived = serializers.BooleanField()
    max_amount = serializers.IntegerField(allow_null=True)
    article_code = serializers.CharField(allow_blank=True)
    backend_id = serializers.CharField(allow_blank=True)
    components = ExportPlanComponentDataSerializer(many=True)


class ExportScreenshotDataSerializer(serializers.Serializer):
    """Serializer for screenshot data in export."""

    name = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    image_content = serializers.CharField(allow_blank=True)
    image_filename = serializers.CharField(allow_blank=True)
    content_type = serializers.CharField(allow_blank=True)


class ExportFileDataSerializer(serializers.Serializer):
    """Serializer for file data in export."""

    name = serializers.CharField()
    file_content = serializers.CharField(allow_blank=True)
    filename = serializers.CharField(allow_blank=True)
    content_type = serializers.CharField(allow_blank=True)


class ExportEndpointDataSerializer(serializers.Serializer):
    """Serializer for endpoint data in export."""

    name = serializers.CharField()
    url = serializers.URLField()


class ExportOrganizationGroupDataSerializer(serializers.Serializer):
    """Serializer for organization group data in export."""

    name = serializers.CharField()
    parent_name = serializers.CharField(allow_null=True)


class ExportTermsOfServiceDataSerializer(serializers.Serializer):
    """Serializer for terms of service data in export."""

    terms_of_service = serializers.CharField(allow_blank=True)
    terms_of_service_link = serializers.URLField(allow_blank=True)
    version = serializers.CharField(allow_blank=True)
    is_active = serializers.BooleanField()
    requires_reconsent = serializers.BooleanField()
    grace_period_days = serializers.IntegerField(allow_null=True)


class OfferingExportDataSerializer(serializers.Serializer):
    """Complete serializer for offering export data structure."""

    offering = ExportOfferingDataSerializer()
    components = ExportComponentDataSerializer(many=True, required=False)
    plans = ExportPlanDataSerializer(many=True, required=False)
    screenshots = ExportScreenshotDataSerializer(many=True, required=False)
    files = ExportFileDataSerializer(many=True, required=False)
    endpoints = ExportEndpointDataSerializer(many=True, required=False)
    organization_groups = ExportOrganizationGroupDataSerializer(
        many=True, required=False
    )
    terms_of_service = ExportTermsOfServiceDataSerializer(many=True, required=False)
    plugin_options = serializers.JSONField(required=False)
    secret_options = serializers.JSONField(required=False)
    resource_options = serializers.JSONField(required=False)


class OfferingImportParametersSerializer(serializers.Serializer):
    """
    Serializer to configure offering import parameters.

    Controls how the offering data should be imported and mapped.
    """

    customer = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.Customer.objects.all(),
        required=False,
        allow_null=True,
        help_text="Target customer for imported offering. If not provided, uses current user's customer",
    )
    category = serializers.SlugRelatedField(
        slug_field="title",
        queryset=models.Category.objects.all(),
        required=False,
        allow_null=True,
        help_text="Target category name for imported offering. If not provided, uses category from export data",
    )
    project = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.Project.objects.all(),
        required=False,
        allow_null=True,
        help_text="Target project for imported offering (optional)",
    )
    import_components = serializers.BooleanField(
        default=True, help_text="Import offering components"
    )
    import_plans = serializers.BooleanField(
        default=True, help_text="Import offering plans"
    )
    import_screenshots = serializers.BooleanField(
        default=True, help_text="Import offering screenshots"
    )
    import_files = serializers.BooleanField(
        default=True, help_text="Import offering files"
    )
    import_endpoints = serializers.BooleanField(
        default=True, help_text="Import offering access endpoints"
    )
    import_organization_groups = serializers.BooleanField(
        default=False,
        help_text="Import organization groups associations (may fail if groups don't exist)",
    )
    import_terms_of_service = serializers.BooleanField(
        default=True, help_text="Import terms of service configurations"
    )
    import_plugin_options = serializers.BooleanField(
        default=True, help_text="Import plugin options"
    )
    import_secret_options = serializers.BooleanField(
        default=False,
        help_text="Import secret options (WARNING: will overwrite existing secrets)",
    )
    overwrite_existing = serializers.BooleanField(
        default=False,
        help_text="Overwrite existing offering if one with the same name exists",
    )

    @extend_schema_field(OfferingExportDataSerializer)
    class OfferingExportDataField(serializers.JSONField):
        pass

    offering_data = OfferingExportDataField(
        help_text="The exported offering data to import"
    )


class OfferingExportResponseSerializer(serializers.Serializer):
    """Serializer for offering export response."""

    offering_uuid = serializers.UUIDField(help_text="UUID of the exported offering")
    offering_name = serializers.CharField(help_text="Name of the exported offering")
    export_data = OfferingExportDataSerializer(
        help_text="Complete export data containing the offering structure"
    )
    exported_components = serializers.ListField(
        child=serializers.CharField(), help_text="List of exported component types"
    )
    export_timestamp = serializers.DateTimeField(
        help_text="Timestamp when the export was completed"
    )


class OfferingImportResponseSerializer(serializers.Serializer):
    """Serializer for offering import response."""

    imported_offering_uuid = serializers.UUIDField(
        help_text="UUID of the imported offering"
    )
    imported_offering_name = serializers.CharField(
        help_text="Name of the imported offering"
    )
    imported_components = serializers.ListField(
        child=serializers.CharField(), help_text="List of imported component types"
    )
    warnings = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of warnings encountered during import",
    )
    import_timestamp = serializers.DateTimeField(
        help_text="Timestamp when the import was completed"
    )


class ResourceProvisioningStatsSerializer(serializers.Serializer):
    offering_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the offering"
    )
    offering_name = serializers.CharField(
        read_only=True, help_text="Name of the offering"
    )
    service_provider_uuid = serializers.UUIDField(
        read_only=True, help_text="UUID of the service provider"
    )
    service_provider_name = serializers.CharField(
        read_only=True, help_text="Name of the service provider"
    )
    provisioning_count = serializers.IntegerField(
        read_only=True, help_text="Total finished provisioning attempts (DONE + ERRED)"
    )
    provisioning_success_count = serializers.IntegerField(
        read_only=True, help_text="Total successful provisioning attempts (DONE)"
    )
    provisioning_error_count = serializers.IntegerField(
        read_only=True, help_text="Total failed provisioning attempts (ERRED)"
    )
    provisioning_in_progress_count = serializers.IntegerField(
        read_only=True, help_text="Total currently in-progress provisioning attempts"
    )
    provisioning_success_rate = serializers.FloatField(
        read_only=True, help_text="Rate of successful provisioning (0.0 to 1.0)"
    )
    avg_provisioning_duration = serializers.FloatField(
        read_only=True,
        help_text="Average duration in seconds from Executing to Terminal state",
    )
    avg_pending_duration = serializers.FloatField(
        read_only=True,
        help_text="Average duration in seconds from Creation to Executing state",
    )


# Demo Presets Serializers


class DemoPresetSerializer(serializers.Serializer):
    """Serializer for demo preset metadata."""

    name = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    version = serializers.CharField(read_only=True)
    entity_counts = serializers.DictField(
        child=serializers.IntegerField(),
        read_only=True,
    )
    scenarios = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )


class DemoPresetLoadRequestSerializer(serializers.Serializer):
    """Request serializer for loading a demo preset."""

    dry_run = serializers.BooleanField(
        default=False,
        help_text="Preview changes without applying them",
    )
    cleanup_first = serializers.BooleanField(
        default=True,
        help_text="Clean up existing data before loading the preset",
    )
    skip_users = serializers.BooleanField(
        default=False,
        help_text="Skip user import/cleanup",
    )
    skip_roles = serializers.BooleanField(
        default=False,
        help_text="Skip role import/cleanup",
    )


class DemoPresetUserSerializer(serializers.Serializer):
    """Serializer for demo preset user credentials."""

    username = serializers.CharField()
    password = serializers.CharField()
    email = serializers.CharField(required=False, allow_blank=True)
    is_staff = serializers.BooleanField(default=False)
    is_support = serializers.BooleanField(default=False)


class DemoPresetLoadResponseSerializer(serializers.Serializer):
    """Response serializer for demo preset loading."""

    success = serializers.BooleanField()
    message = serializers.CharField()
    output = serializers.CharField(required=False, allow_blank=True)
    users = DemoPresetUserSerializer(many=True, required=False)


# Site Agent Configuration Serializers


class SiteAgentConfigGenerationSerializer(serializers.Serializer):
    """Request serializer for site agent configuration generation."""

    offering_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        help_text="List of SLURM offering UUIDs to include in configuration",
    )
    include_policy_settings = serializers.BooleanField(
        default=True,
        help_text="Include SLURM periodic usage policy settings in configuration",
    )
    waldur_api_url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text="Waldur API URL (defaults to current server URL)",
    )
    timezone = serializers.CharField(
        required=False,
        default="UTC",
        help_text="Timezone for the site agent",
    )

    def validate_offering_uuids(self, value):
        """Validate that all offerings exist and belong to the service provider."""
        service_provider = self.context.get("service_provider")
        if not service_provider:
            raise serializers.ValidationError("Service provider context is required.")

        # Find valid offerings that belong to this service provider and are SLURM type
        valid_offerings = models.Offering.objects.filter(
            uuid__in=value,
            customer=service_provider.customer,
            type=SITE_AGENT_OFFERING,
        )

        # Use .hex for consistent UUID comparison (no dashes)
        valid_uuids = set(o.uuid.hex for o in valid_offerings)
        provided_uuids = set(u.hex for u in value)
        invalid_uuids = provided_uuids - valid_uuids

        if invalid_uuids:
            # Format UUIDs with dashes for display
            formatted_invalid = {str(u) for u in value if u.hex in invalid_uuids}
            raise serializers.ValidationError(
                f"Invalid, non-SLURM, or unauthorized offerings: {formatted_invalid}"
            )

        # Store offerings for later use
        self.context["validated_offerings"] = list(valid_offerings)
        return value


class ResourceMissingUsageSerializer(serializers.Serializer):
    """Serializer for resources with missing usage reports."""

    uuid = serializers.UUIDField(help_text="UUID of the resource")
    name = serializers.CharField(help_text="Name of the resource")
    state = serializers.CharField(help_text="Current state of the resource")
    created = serializers.DateTimeField(help_text="Creation date of the resource")
    offering_name = serializers.CharField(help_text="Name of the offering")
    offering_uuid = serializers.UUIDField(help_text="UUID of the offering")
    provider_name = serializers.CharField(help_text="Name of the service provider")
    provider_uuid = serializers.UUIDField(help_text="UUID of the service provider")
    customer_name = serializers.CharField(help_text="Name of the customer organization")
    customer_uuid = serializers.UUIDField(help_text="UUID of the customer organization")
    project_name = serializers.CharField(help_text="Name of the project")
    project_uuid = serializers.UUIDField(help_text="UUID of the project")
    last_usage_date = serializers.DateTimeField(
        allow_null=True, help_text="Date of the last usage report"
    )
    days_since_last_report = serializers.IntegerField(
        allow_null=True, help_text="Number of days since last usage report"
    )


class DailyOrderStatsSerializer(serializers.Serializer):
    """Serializer for daily aggregated order statistics."""

    date = serializers.DateField(help_text="Date of the statistics")
    total = serializers.IntegerField(help_text="Total number of orders")
    total_cost = serializers.DecimalField(
        max_digits=20,
        decimal_places=2,
        allow_null=True,
        help_text="Total cost of orders",
    )
    revenue = serializers.DecimalField(
        max_digits=20,
        decimal_places=2,
        allow_null=True,
        help_text="Revenue from create/update orders",
    )
    by_state = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Order counts grouped by state",
    )
    by_type = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Order counts grouped by type",
    )


class OrderStatsSummarySerializer(serializers.Serializer):
    """Serializer for order summary statistics."""

    total = serializers.IntegerField(help_text="Total number of orders")
    total_cost = serializers.DecimalField(
        max_digits=20,
        decimal_places=2,
        allow_null=True,
        help_text="Total cost of orders",
    )
    total_revenue = serializers.DecimalField(
        max_digits=20,
        decimal_places=2,
        allow_null=True,
        help_text="Total revenue from create/update orders",
    )
    pending = serializers.IntegerField(help_text="Number of pending orders")
    executing = serializers.IntegerField(help_text="Number of executing orders")
    done = serializers.IntegerField(help_text="Number of completed orders")
    erred = serializers.IntegerField(help_text="Number of erred orders")
    canceled = serializers.IntegerField(help_text="Number of canceled orders")
    rejected = serializers.IntegerField(help_text="Number of rejected orders")


class OrderStatsResponseSerializer(serializers.Serializer):
    """Comprehensive order statistics response."""

    summary = OrderStatsSummarySerializer(help_text="Summary statistics")
    by_state = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Total order counts grouped by state",
    )
    by_type = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Total order counts grouped by type",
    )
    daily = DailyOrderStatsSerializer(many=True, help_text="Daily breakdown")


# Maintenance reporting serializers


class MaintenanceStatsSummarySerializer(serializers.Serializer):
    """Summary statistics for maintenance announcements."""

    total = serializers.IntegerField(
        help_text="Total number of maintenance announcements"
    )
    active = serializers.IntegerField(
        help_text="Number of currently active maintenances"
    )
    scheduled = serializers.IntegerField(help_text="Number of scheduled maintenances")
    completed = serializers.IntegerField(help_text="Number of completed maintenances")
    average_duration_hours = serializers.FloatField(
        allow_null=True, help_text="Average duration of completed maintenances in hours"
    )
    on_time_completion_rate = serializers.FloatField(
        allow_null=True, help_text="Percentage of maintenances completed on time"
    )


class DailyMaintenanceStatsSerializer(serializers.Serializer):
    """Daily maintenance statistics."""

    date = serializers.DateField(help_text="Date")
    count = serializers.IntegerField(help_text="Number of maintenances on this day")
    by_state = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Maintenance counts grouped by state",
    )


class MaintenanceProviderStatsSerializer(serializers.Serializer):
    """Maintenance statistics per provider."""

    uuid = serializers.CharField(help_text="Service provider UUID")
    name = serializers.CharField(help_text="Service provider name")
    total = serializers.IntegerField(help_text="Total maintenances")
    active = serializers.IntegerField(help_text="Active maintenances")
    scheduled = serializers.IntegerField(help_text="Scheduled maintenances")
    completed = serializers.IntegerField(help_text="Completed maintenances")


class MaintenanceStatsResponseSerializer(serializers.Serializer):
    """Comprehensive maintenance statistics response for reporting dashboards."""

    summary = MaintenanceStatsSummarySerializer(help_text="Summary statistics")
    by_state = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Total counts grouped by state",
    )
    by_type = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Total counts grouped by maintenance type",
    )
    by_impact_level = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Total counts grouped by max impact level",
    )
    daily = DailyMaintenanceStatsSerializer(many=True, help_text="Daily breakdown")
    providers = MaintenanceProviderStatsSerializer(
        many=True, help_text="Statistics per provider"
    )


# Provider reporting serializers


class ProviderResourceStatsSerializer(serializers.Serializer):
    """Resource statistics for a service provider."""

    total = serializers.IntegerField(help_text="Total number of resources")
    by_state = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Resource counts grouped by state",
    )
    by_offering = serializers.ListField(
        child=serializers.DictField(),
        help_text="Resource counts grouped by offering",
    )
    monthly = serializers.ListField(
        child=serializers.DictField(),
        help_text="Monthly resource counts",
    )


class ProviderCustomerStatsSerializer(serializers.Serializer):
    """Customer statistics for a service provider."""

    total = serializers.IntegerField(help_text="Total number of customers")
    new_this_month = serializers.IntegerField(help_text="New customers this month")
    top_by_revenue = serializers.ListField(
        child=serializers.DictField(),
        help_text="Top customers by revenue",
    )
    top_by_resources = serializers.ListField(
        child=serializers.DictField(),
        help_text="Top customers by resource count",
    )
    monthly = serializers.ListField(
        child=serializers.DictField(),
        help_text="Monthly customer counts",
    )


class ProviderOfferingStatsSerializer(serializers.Serializer):
    """Offering performance statistics for a service provider."""

    offerings = serializers.ListField(
        child=serializers.DictField(),
        help_text="Offering statistics including resources, revenue, and utilization",
    )


# Report summary serializers


class OfferingCostsSummarySerializer(serializers.Serializer):
    """Summary statistics for offering costs."""

    total_cost = serializers.DecimalField(
        max_digits=20,
        decimal_places=2,
        help_text="Total cost of all active resources across all offerings",
    )
    offering_count = serializers.IntegerField(
        help_text="Number of offerings with active resources"
    )
    average_cost = serializers.DecimalField(
        max_digits=20,
        decimal_places=2,
        help_text="Average cost per offering",
    )


class ResourcesGeographySummarySerializer(serializers.Serializer):
    """Summary statistics for resource geographic distribution."""

    total_resources = serializers.IntegerField(
        help_text="Total number of active resources"
    )
    countries_count = serializers.IntegerField(
        help_text="Number of countries with active resources"
    )
    org_groups_count = serializers.IntegerField(
        help_text="Number of organization groups with active resources"
    )
    offerings_count = serializers.IntegerField(
        help_text="Number of offerings with active resources"
    )


class CustomerMemberSummarySerializer(serializers.Serializer):
    """Summary statistics for customer members."""

    total_organizations = serializers.IntegerField(
        help_text="Total number of organizations"
    )
    total_members = serializers.IntegerField(
        help_text="Total number of members across all organizations"
    )
    organizations_with_resources = serializers.IntegerField(
        help_text="Number of organizations with active resources"
    )
    average_members_per_org = serializers.IntegerField(
        help_text="Average number of members per organization"
    )


class ProjectClassificationSummarySerializer(serializers.Serializer):
    """Summary statistics for project classification."""

    total_projects = serializers.IntegerField(help_text="Total number of projects")
    academic_projects = serializers.IntegerField(
        help_text="Number of academic projects (industry_flag=False)"
    )
    industry_projects = serializers.IntegerField(
        help_text="Number of industry projects (industry_flag=True)"
    )


# Extended stats report serializers


class ResourceUsageByOrgTypeSerializer(serializers.Serializer):
    """Resource usage grouped by creator's organization type."""

    organization_type = serializers.CharField(
        allow_null=True, help_text="SCHAC organization type URN"
    )
    component_type = serializers.CharField(help_text="Component type (e.g., cpu, gpu)")
    usage = serializers.DecimalField(
        max_digits=20, decimal_places=2, help_text="Total usage for this component"
    )
    resource_count = serializers.IntegerField(help_text="Number of resources")


class ResourceUsageByCustomerSerializer(serializers.Serializer):
    """Full resource breakdown per customer."""

    customer_uuid = serializers.UUIDField(help_text="UUID of the customer")
    customer_name = serializers.CharField(help_text="Name of the customer")
    customer_abbreviation = serializers.CharField(
        allow_null=True, help_text="Abbreviation of the customer"
    )
    resources_ok = serializers.IntegerField(help_text="Number of OK resources")
    resources_erred = serializers.IntegerField(help_text="Number of erred resources")
    resources_total = serializers.IntegerField(
        help_text="Total number of active resources"
    )
    total_cost = serializers.DecimalField(
        max_digits=20, decimal_places=2, help_text="Total cost of resources"
    )
    usages = serializers.DictField(
        child=serializers.DecimalField(max_digits=20, decimal_places=2),
        help_text="Component usages keyed by component type",
    )
    limits = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Resource limits keyed by limit name",
    )


class ResourceUsageByAffiliationSerializer(serializers.Serializer):
    """Resource usage grouped by creator's affiliation."""

    affiliation = serializers.CharField(help_text="User affiliation value")
    component_type = serializers.CharField(help_text="Component type")
    total_usage = serializers.DecimalField(
        max_digits=20, decimal_places=2, help_text="Total usage"
    )
    total_cost = serializers.DecimalField(
        max_digits=20, decimal_places=2, help_text="Total cost"
    )
    resource_count = serializers.IntegerField(help_text="Number of resources")


class AggregatedUsageTrendSerializer(serializers.Serializer):
    """Aggregated usage data per month for trends reporting."""

    period = serializers.CharField(help_text="Period in YYYY-MM format")
    year = serializers.IntegerField(help_text="Year")
    month = serializers.IntegerField(help_text="Month (1-12)")
    total_usage = serializers.DecimalField(
        max_digits=20, decimal_places=2, help_text="Total usage across all components"
    )
    resource_count = serializers.IntegerField(
        help_text="Number of distinct resources with usage"
    )
    component_count = serializers.IntegerField(
        help_text="Number of component usage records"
    )


class OpenStackInstanceReportSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(help_text="Instance UUID")
    name = serializers.CharField(help_text="Instance name")
    created = serializers.DateTimeField(help_text="Creation timestamp")
    cores = serializers.IntegerField(help_text="Number of vCPUs")
    ram = serializers.IntegerField(help_text="RAM in MiB")
    disk = serializers.IntegerField(help_text="Root disk in MiB")
    flavor_name = serializers.CharField(help_text="Flavor name")
    flavor_disk = serializers.IntegerField(help_text="Flavor disk in MiB")
    image_name = serializers.CharField(help_text="Image name")
    hypervisor_hostname = serializers.CharField(help_text="Hypervisor hostname")
    runtime_state = serializers.CharField(
        help_text="Runtime state (e.g. ACTIVE, SHUTOFF)"
    )
    state = serializers.CharField(help_text="Provisioning state")
    availability_zone_name = serializers.CharField(
        allow_null=True, help_text="Availability zone name"
    )
    start_time = serializers.DateTimeField(
        allow_null=True, help_text="Last start time of the VM"
    )
    service_settings_uuid = serializers.UUIDField(help_text="Cluster UUID")
    service_settings_name = serializers.CharField(help_text="Cluster name")
    tenant_uuid = serializers.UUIDField(help_text="Tenant UUID")
    tenant_name = serializers.CharField(help_text="Tenant name")
    project_uuid = serializers.UUIDField(help_text="Project UUID")
    project_name = serializers.CharField(help_text="Project name")
    customer_uuid = serializers.UUIDField(help_text="Customer UUID")
    customer_name = serializers.CharField(help_text="Customer name")
    customer_abbreviation = serializers.CharField(help_text="Customer abbreviation")
    volume_count = serializers.IntegerField(help_text="Number of attached volumes")
    total_volume_size_mb = serializers.IntegerField(
        help_text="Total attached volume size in MiB"
    )
    floating_ip_count = serializers.IntegerField(help_text="Number of floating IPs")
    port_count = serializers.IntegerField(help_text="Number of ports")
    internal_ips = serializers.ListField(
        child=serializers.CharField(), help_text="List of internal IP addresses"
    )
    external_ips = serializers.ListField(
        child=serializers.CharField(), help_text="List of external IP addresses"
    )


class OpenStackInstanceAggregateSerializer(serializers.Serializer):
    group_key = serializers.CharField(help_text="Group key value")
    group_label = serializers.CharField(help_text="Human-readable group label")
    instance_count = serializers.IntegerField(help_text="Number of instances")
    total_cores = serializers.IntegerField(help_text="Total vCPUs")
    total_ram_mb = serializers.IntegerField(help_text="Total RAM in MiB")
    total_disk_mb = serializers.IntegerField(help_text="Total disk in MiB")
    total_volume_size_mb = serializers.IntegerField(
        help_text="Total attached volume size in MiB"
    )
    total_floating_ips = serializers.IntegerField(
        help_text="Total number of floating IPs"
    )


class ArticleCodeUpdatePreviewSerializer(serializers.Serializer):
    search = serializers.CharField(
        max_length=30,
        min_length=1,
        help_text="Substring to search for in article codes.",
    )
    replace = serializers.CharField(
        max_length=30,
        allow_blank=True,
        default="",
        help_text="Replacement string.",
    )
    offering_category_uuid = serializers.UUIDField(
        required=False,
        help_text="Filter by offering category UUID.",
    )
    offering_customer_uuid = serializers.UUIDField(
        required=False,
        help_text="Filter by service provider (customer) UUID.",
    )
    offering_state = NaturalChoiceField(
        choices=OfferingStates.CHOICES,
        required=False,
        help_text="Filter by offering state.",
    )
    offering_name = serializers.CharField(
        required=False,
        help_text="Filter by offering name (case-insensitive substring match).",
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        search = attrs["search"]
        replace = attrs.get("replace", "")
        max_length = 30
        # Check that replacement won't produce article codes exceeding max_length.
        # Worst case: the entire article code is the search string repeated.
        if len(replace) > len(search):
            growth = len(replace) - len(search)
            max_occurrences = max_length // len(search)
            if max_length + growth * max_occurrences > max_length:
                # We can't predict exact length without seeing real data,
                # so we validate individual results in the view.
                pass
        return attrs


class ArticleCodeUpdateApplySerializer(ArticleCodeUpdatePreviewSerializer):
    component_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        help_text="UUIDs of components to update (from preview results).",
    )


class ArticleCodeUpdatePreviewItemSerializer(serializers.Serializer):
    component_uuid = serializers.UUIDField()
    component_type = serializers.CharField()
    component_name = serializers.CharField()
    offering_uuid = serializers.UUIDField()
    offering_name = serializers.CharField()
    offering_customer_name = serializers.CharField()
    old_article_code = serializers.CharField()
    new_article_code = serializers.CharField()


class ArticleCodeUpdateApplyResponseSerializer(serializers.Serializer):
    updated_count = serializers.IntegerField()
