from typing import cast

from django.conf import settings
from django.core import validators as django_validators
from django.core.exceptions import MultipleObjectsReturned
from django.db import transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions, serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core.enums import CoreStates
from waldur_core.core.validators import (
    BackendURLValidator,
    is_valid_ipv4_cidr,
    is_valid_ipv6_cidr,
)
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import Project, ServiceSettings, VirtualMachine
from waldur_openstack import models as openstack_models
from waldur_openstack import serializers as openstack_serializers
from waldur_openstack.serializers import (
    _validate_instance_security_groups,
    validate_security_group_rule,
)
from waldur_rancher.enums import (
    AGENT_ROLE,
    RANCHER_TEMPLATE_QUESTION_TYPE,
    ROLE_CHOICES,
    SERVER_ROLE,
    RoleScopeType,
)

from . import models, utils, validators


class RancherServiceSettingsSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = (
            "backend_url",
            "username",
            "password",
            "private_registry_url",
            "private_registry_user",
            "private_registry_password",
            "vault_host",
            "vault_port",
            "vault_token",
            "vault_verify",
            "keycloak_url",
            "keycloak_realm",
            "keycloak_user_realm",
            "keycloak_username",
            "keycloak_password",
            "keycloak_sync_frequency",
            "argocd_k8s_namespace",
            "argocd_k8s_kubeconfig",
        )

    backend_url = serializers.CharField(
        max_length=200, label=_("Rancher server URL"), validators=[BackendURLValidator]
    )

    username = serializers.CharField(max_length=100, label=_("Rancher access key"))

    password = serializers.CharField(max_length=100, label=_("Rancher secret key"))

    base_image_name = serializers.CharField(
        source="options.base_image_name", label=_("Base image name")
    )

    k8s_version = serializers.CharField(
        source="options.k8s_version",
        help_text=_("Kubernetes version"),
        required=False,
    )

    cloud_init_template = serializers.CharField(
        source="options.cloud_init_template",
        label=_("Cloud init template"),
        required=False,
    )

    private_registry_url = serializers.CharField(
        source="options.private_registry_url",
        help_text=_("URL of a private registry for a cluster"),
        required=False,
    )

    private_registry_user = serializers.CharField(
        source="options.private_registry_user",
        help_text=_("Username for accessing a private registry"),
        required=False,
    )

    private_registry_password = serializers.CharField(
        source="options.private_registry_password",
        help_text=_("Password for accessing a private registry"),
        required=False,
    )

    allocate_floating_ip_to_all_nodes = serializers.BooleanField(
        source="options.allocate_floating_ip_to_all_nodes",
        help_text=_(
            "If True, on provisioning a floating IP will be allocated to each of the nodes"
        ),
        required=False,
    )

    management_tenant_uuid = serializers.UUIDField(
        source="options.management_tenant_uuid",
        help_text=_("Tenant where Rancher management is running"),
        required=False,
    )

    management_tenant_access_port = serializers.IntegerField(
        source="options.management_tenant_access_port",
        help_text=_("Management tenant access port"),
        required=False,
    )

    vault_host = serializers.CharField(
        source="options.vault_host",
        help_text=_("Host of the Vault server"),
        required=False,
    )

    vault_port = serializers.IntegerField(
        source="options.vault_port",
        help_text=_("Port of the Vault server"),
        required=False,
    )
    vault_token = serializers.CharField(
        source="options.vault_token",
        help_text=_("Token for the Vault server"),
        required=False,
    )
    vault_tls_verify = serializers.BooleanField(
        source="options.vault_tls_verify",
        help_text=_("Whether to verify the Vault server certificate"),
        required=False,
        default=True,
    )

    keycloak_url = serializers.CharField(
        source="options.keycloak_url",
        help_text=_("URL of the Keycloak server"),
        required=False,
    )

    keycloak_realm = serializers.CharField(
        source="options.keycloak_realm",
        help_text=_("Keycloak realm for Rancher"),
        required=False,
    )

    keycloak_user_realm = serializers.CharField(
        source="options.keycloak_user_realm",
        help_text=_("Keycloak user realm for auth"),
        default="master",
        required=False,
    )

    keycloak_username = serializers.CharField(
        source="options.keycloak_username",
        help_text=_("Username of the Keycloak integration user"),
        required=False,
    )

    keycloak_password = serializers.CharField(
        source="options.keycloak_password",
        help_text=_("Password of the Keycloak integration user"),
        required=False,
    )

    keycloak_sync_frequency = serializers.IntegerField(
        source="options.keycloak_sync_frequency",
        help_text=_("Frequency in minutes for syncing Keycloak users"),
        required=False,
        default=15,
    )

    keycloak_ssl_verify = serializers.BooleanField(
        source="options.keycloak_ssl_verify",
        help_text=_("Indicates whether verify SSL certificates"),
        required=False,
        default=True,
    )

    argocd_k8s_namespace = serializers.CharField(
        source="options.argocd_k8s_namespace",
        help_text=_("Namespace where ArgoCD is deployed"),
        required=False,
    )
    argocd_k8s_kubeconfig = serializers.CharField(
        source="options.argocd_k8s_kubeconfig",
        help_text=_("Kubeconfig with access to namespace where ArgoCD is deployed"),
        required=False,
    )

    node_disk_driver = serializers.CharField(
        source="options.node_disk_driver",
        required=False,
        help_text=_("OpenStack disk driver for Rancher nodes"),
    )

    def validate_management_tenant_uuid(self, tenant_uuid):
        if not filter_queryset_for_user(
            openstack_models.Tenant.objects.filter(uuid=tenant_uuid),
            self.context["request"].user,
        ):
            raise serializers.ValidationError(
                _("User has not permissions for tenant %s") % tenant_uuid
            )
        return tenant_uuid


class DataVolumeSerializer(
    structure_serializers.PermissionFieldFilteringMixin, serializers.Serializer
):
    size = serializers.IntegerField()
    volume_type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=openstack_models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        required=False,
    )
    filesystem = serializers.CharField(required=False)
    mount_point = serializers.CharField(
        required=True,
    )

    def get_filtered_field_names(self):
        return ["volume_type"]


class RancherBaseNodeSerializer(
    structure_serializers.PermissionFieldFilteringMixin,
    serializers.HyperlinkedModelSerializer,
):
    subnet = serializers.HyperlinkedRelatedField(
        view_name="openstack-subnet-detail",
        queryset=openstack_models.SubNet.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        write_only=True,
    )
    flavor = serializers.HyperlinkedRelatedField(
        view_name="openstack-flavor-detail",
        queryset=openstack_models.Flavor.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        write_only=True,
        required=False,
    )
    system_volume_size = serializers.IntegerField(
        write_only=True,
        required=False,
        validators=[
            django_validators.MinValueValidator(
                lambda: settings.WALDUR_RANCHER["SYSTEM_VOLUME_MIN_SIZE"]
            )
        ],
    )
    system_volume_type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=openstack_models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        required=False,
        write_only=True,
    )
    data_volumes = DataVolumeSerializer(many=True, write_only=True, required=False)
    memory = serializers.IntegerField(write_only=True, required=False)
    cpu = serializers.IntegerField(write_only=True, required=False)
    role = serializers.ChoiceField(choices=ROLE_CHOICES)
    tenant = serializers.HyperlinkedRelatedField(
        queryset=openstack_models.Tenant.objects.all(),
        view_name="openstack-tenant-detail",
        lookup_field="uuid",
        required=False,
        write_only=True,
    )

    class Meta:
        model = models.Node
        read_only_fields = (
            "error_message",
            "initial_data",
            "runtime_state",
            "k8s_version",
            "docker_version",
            "cpu_allocated",
            "cpu_total",
            "ram_allocated",
            "ram_total",
            "pods_allocated",
            "pods_total",
            "labels",
            "annotations",
        )
        exclude = ("state",)

    def get_filtered_field_names(self):
        return ("subnet", "flavor", "system_volume_type")

    def get_fields(self):
        fields = super().get_fields()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if (
            settings.WALDUR_RANCHER["DISABLE_DATA_VOLUME_CREATION"]
            and "data_volumes" in fields
        ):
            del fields["data_volumes"]
        return fields


class RancherNestedNodeSerializer(RancherBaseNodeSerializer):
    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(), read_only=True
    )

    class Meta(RancherBaseNodeSerializer.Meta):
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "rancher-node-detail"},
            "cluster": {"lookup_field": "uuid", "view_name": "rancher-cluster-detail"},
        }
        exclude = RancherBaseNodeSerializer.Meta.exclude + (
            "cluster",
            "name",
        )


class RancherNestedSecurityGroupSerializer(
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = openstack_models.SecurityGroup
        fields = ("url",)
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "openstack-sgp-detail"}
        }


class RancherSecurityGroupRequestSerializer(serializers.Serializer):
    url = serializers.HyperlinkedRelatedField(
        queryset=openstack_models.SecurityGroup.objects.all(),
        lookup_field="uuid",
        view_name="openstack-sgp-detail",
    )

    def to_internal_value(self, data):
        return super().to_internal_value(data)["url"]


class RancherNestedPublicIPSerializer(serializers.HyperlinkedModelSerializer):
    ip_address = serializers.IPAddressField(
        source="floating_ip.address", read_only=True
    )
    external_ip_address = serializers.IPAddressField(
        source="floating_ip.external_address", read_only=True
    )
    floating_ip = serializers.HyperlinkedRelatedField(
        read_only=True, view_name="openstack-fip-detail", lookup_field="uuid"
    )
    floating_ip_uuid = serializers.UUIDField(source="floating_ip.uuid", read_only=True)

    class Meta:
        model = models.ClusterPublicIP
        fields = (
            "floating_ip",
            "floating_ip_uuid",
            "ip_address",
            "external_ip_address",
        )


class RancherClusterSerializer(
    structure_serializers.SshPublicKeySerializerMixin,
    structure_serializers.BaseResourceSerializer,
):
    tenant = serializers.HyperlinkedRelatedField(
        queryset=openstack_models.Tenant.objects.all(),
        view_name="openstack-tenant-detail",
        lookup_field="uuid",
        required=False,
    )

    tenant_uuid = serializers.UUIDField(read_only=True, source="tenant.uuid")

    vm_project = serializers.HyperlinkedRelatedField(
        queryset=Project.objects.all(),
        view_name="project-detail",
        lookup_field="uuid",
        required=False,
        allow_null=True,
    )

    name = serializers.CharField(
        max_length=150, validators=[validators.ClusterNameValidator]
    )
    nodes = RancherNestedNodeSerializer(many=True, source="node_set")

    install_longhorn = serializers.BooleanField(
        default=False,
        help_text=_(
            "Longhorn is a distributed block storage deployed on top of Kubernetes cluster"
        ),
    )

    management_security_group = serializers.HyperlinkedRelatedField(
        read_only=True, view_name="openstack-sgp-detail", lookup_field="uuid"
    )

    public_ips = RancherNestedPublicIPSerializer(many=True, read_only=True)

    router_ips = serializers.SerializerMethodField()

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Cluster
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "nodes",
            "tenant",
            "tenant_uuid",
            "vm_project",
            "runtime_state",
            "ssh_public_key",
            "install_longhorn",
            "management_security_group",
            "public_ips",
            "capacity",
            "requested",
            "kubernetes_version",
            "router_ips",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "runtime_state",
                "kubernetes_version",
                "capacity",
                "requested",
            )
        )
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + ("nodes", "tenant")
        )
        extra_kwargs = dict(
            cluster={
                "view_name": "rancher-cluster-detail",
                "lookup_field": "uuid",
            },
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs,
        )

    def get_fields(self):
        fields = super().get_fields()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if (
            settings.WALDUR_RANCHER["DISABLE_SSH_KEY_INJECTION"]
            and "ssh_public_key" in fields
        ):
            del fields["ssh_public_key"]
        try:
            request = self.context["view"].request
            user = request.user
        except (KeyError, AttributeError):
            return fields
        for field in ("vm_project", "tenant"):
            if field in fields:
                field = cast(serializers.RelatedField, fields[field])
                field.queryset = filter_queryset_for_user(
                    cast(QuerySet, field.queryset), user
                )
        return fields

    def validate_vm_project(self, vm_project: Project) -> Project:
        """Validate that the vm_project is not soft-deleted."""
        if vm_project and vm_project.is_removed:
            raise serializers.ValidationError(
                _("Cannot assign VMs to terminated projects.")
            )
        return vm_project

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs = super().validate(attrs)
        nodes = attrs["node_set"]
        name = attrs["name"]
        service_settings: ServiceSettings = attrs["service_settings"]
        attrs["settings"] = service_settings
        attrs.setdefault("vm_project", attrs["project"])
        vm_project: Project = attrs["vm_project"]
        ssh_public_key = attrs.pop("ssh_public_key", None)

        clusters = models.Cluster.objects.filter(settings=service_settings, name=name)
        if self.instance:
            clusters = clusters.exclude(id=self.instance.id)
        if clusters.exists():
            raise serializers.ValidationError(_("Name is not unique."))

        tenant: openstack_models.Tenant | None = attrs.get("tenant")
        if not tenant:
            for node in nodes:
                if not node.get("tenant"):
                    raise exceptions.ValidationError(
                        "Either cluster or node tenant should be specified."
                    )
            # TODO: figure out which tenant cluster should be linked to in case of multiple tenants
            first_tenant = nodes[0]["tenant"]
            attrs["tenant"] = first_tenant
        else:
            for node in nodes:
                if node.get("tenant"):
                    raise exceptions.ValidationError(
                        "Either cluster or node tenant should be specified."
                    )
        security_groups = attrs.pop("security_groups", [])
        if tenant and security_groups:
            _validate_instance_security_groups(security_groups, tenant)
        utils.expand_added_nodes(
            name,
            nodes,
            vm_project,
            service_settings,
            tenant,
            ssh_public_key,
            security_groups,
        )
        return attrs

    def validate_nodes(self, nodes):
        if len([node for node in nodes if node["role"] == SERVER_ROLE]) not in [
            1,
            3,
            5,
        ]:
            raise serializers.ValidationError(
                _(
                    "Total count of server nodes must be 1, 3 or 5. You have got %s nodes."
                )
                % len(nodes)
            )

        if not len([node for node in nodes if node["role"] == AGENT_ROLE]):
            raise serializers.ValidationError(_("Count of agent nodes must be min 1."))

        return nodes

    def get_router_ips(self, cluster: models.Cluster) -> list:
        if not cluster.tenant:
            return []

        router_ips = []
        for router in cluster.tenant.routers.all():
            if not router.fixed_ips:
                continue
            router_ips.extend(router.fixed_ips)

        return router_ips


class RancherClusterCreateSerializer(RancherClusterSerializer):
    security_groups = RancherSecurityGroupRequestSerializer(many=True, required=False)

    class Meta(RancherClusterSerializer.Meta):
        fields = RancherClusterSerializer.Meta.fields + ("security_groups",)


class RancherNodeSerializer(serializers.HyperlinkedModelSerializer):
    instance = core_serializers.GenericRelatedField(
        related_models=VirtualMachine.get_all_models(),
        required=True,
    )
    resource_type = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    service_settings_name = serializers.CharField(
        read_only=True, source="service_settings.name"
    )
    service_settings_uuid = serializers.UUIDField(
        read_only=True, source="service_settings.uuid"
    )
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    cluster_name = serializers.CharField(read_only=True, source="cluster.name")
    cluster_uuid = serializers.UUIDField(read_only=True, source="cluster.uuid")
    instance_name = serializers.CharField(read_only=True, source="instance.name")
    instance_uuid = serializers.UUIDField(read_only=True, source="instance.uuid")
    instance_marketplace_uuid = serializers.UUIDField(
        read_only=True, source="instance.marketplace_uuid"
    )

    class Meta:
        model = models.Node
        fields = (
            "uuid",
            "url",
            "created",
            "modified",
            "name",
            "backend_id",
            "project_uuid",
            "service_settings_name",
            "service_settings_uuid",
            "resource_type",
            "state",
            "cluster",
            "cluster_name",
            "cluster_uuid",
            "instance",
            "instance_name",
            "instance_uuid",
            "instance_marketplace_uuid",
            "role",
            "k8s_version",
            "docker_version",
            "cpu_allocated",
            "cpu_total",
            "ram_allocated",
            "ram_total",
            "pods_allocated",
            "pods_total",
            "labels",
            "annotations",
            "runtime_state",
        )
        read_only_fields = (
            "backend_id",
            "k8s_version",
            "docker_version",
            "cpu_allocated",
            "cpu_total",
            "ram_allocated",
            "ram_total",
            "pods_allocated",
            "pods_total",
            "labels",
            "annotations",
            "runtime_state",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "rancher-node-detail"},
            "cluster": {"lookup_field": "uuid", "view_name": "rancher-cluster-detail"},
        }

    @extend_schema_field(serializers.ChoiceField(choices=CoreStates.labels))
    def get_state(self, obj):
        return obj.get_state_display()

    def validate(self, attrs):
        instance = cast(openstack_models.Instance, attrs["instance"])

        if models.Node.objects.filter(
            instance=instance,
        ).exists():
            raise serializers.ValidationError(
                {"instance": "The selected instance is already in use."}
            )

        attrs["name"] = instance.name

        return super().validate(attrs)

    def get_resource_type(self, obj) -> str:
        return "Rancher.Node"


class RancherCreateNodeSerializer(
    structure_serializers.SshPublicKeySerializerMixin, RancherBaseNodeSerializer
):
    class Meta:
        model = models.Node
        fields = (
            "cluster",
            "role",
            "system_volume_size",
            "system_volume_type",
            "memory",
            "cpu",
            "subnet",
            "flavor",
            "data_volumes",
            "ssh_public_key",
            "tenant",
            "uuid",
        )
        extra_kwargs = {
            "cluster": {"lookup_field": "uuid", "view_name": "rancher-cluster-detail"}
        }

    def validate(self, attrs):
        # autoexpand_tenant is available for the Managed Rancher only
        # because the feature is not implemented for the regular Rancher
        autoexpand_tenant = attrs.pop("autoexpand_tenant", False)
        attrs = super().validate(attrs)
        cluster: models.Cluster = attrs["cluster"]
        ssh_public_key = attrs.pop("ssh_public_key", None)
        node = attrs
        node_tenant: openstack_models.Tenant | None = attrs.get("tenant")
        if (not cluster.tenant and not node_tenant) or (node_tenant and cluster.tenant):
            raise serializers.ValidationError(
                _("Tenant should be specified either for node or cluster.")
            )
        if node_tenant:
            if node_tenant.id not in cluster.linked_tenant_ids:
                raise serializers.ValidationError(
                    _("Tenant should be one of already connected ones.")
                )
        utils.expand_added_nodes(
            cluster.name,
            [node],
            cluster.vm_project,
            cluster.service_settings,
            node_tenant or cluster.tenant,
            ssh_public_key,
            autoexpand_tenant=autoexpand_tenant,
        )
        return attrs


class LinkOpenstackSerializer(serializers.Serializer):
    instance = serializers.HyperlinkedRelatedField(
        view_name="openstack-instance-detail",
        queryset=openstack_models.Instance.objects.all(),
        lookup_field="uuid",
        write_only=True,
    )


class RancherCatalogSerializer(serializers.HyperlinkedModelSerializer):
    scope = core_serializers.GenericRelatedField()

    class Meta:
        model = models.Catalog
        fields = (
            "uuid",
            "url",
            "created",
            "modified",
            "name",
            "description",
            "catalog_url",
            "branch",
            "commit",
            "runtime_state",
            "scope",
            "scope_type",
        )
        read_only_fields = ("runtime_state", "commit")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "rancher-catalog-detail"},
        }


class RancherCatalogCreateSerializer(RancherCatalogSerializer):
    class Meta(RancherCatalogSerializer.Meta):
        fields = RancherCatalogSerializer.Meta.fields + ("username", "password")


class RancherCatalogUpdateSerializer(RancherCatalogCreateSerializer):
    class Meta(RancherCatalogSerializer.Meta):
        read_only_fields = RancherCatalogSerializer.Meta.read_only_fields + ("scope",)


class RancherNestedNamespaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Namespace
        fields = (
            "url",
            "uuid",
            "name",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "rancher-namespace-detail"},
        }


class RancherProjectSerializer(structure_serializers.BasePropertySerializer):
    namespaces = RancherNestedNamespaceSerializer(many=True)

    class Meta:
        model = models.Project
        view_name = "rancher-project-detail"
        fields = (
            "url",
            "uuid",
            "name",
            "description",
            "created",
            "modified",
            "runtime_state",
            "cluster",
            "namespaces",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "cluster": {"lookup_field": "uuid", "view_name": "rancher-cluster-detail"},
        }


class RancherNamespaceSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Namespace
        view_name = "rancher-namespace-detail"
        fields = (
            "url",
            "uuid",
            "name",
            "created",
            "modified",
            "runtime_state",
            "project",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "project": {"lookup_field": "uuid", "view_name": "rancher-project-detail"},
        }


class RancherTemplateSerializer(structure_serializers.BasePropertySerializer):
    catalog_name = serializers.ReadOnlyField(source="catalog.name")

    class Meta:
        model = models.Template
        view_name = "rancher-template-detail"
        fields = (
            "url",
            "uuid",
            "name",
            "description",
            "created",
            "modified",
            "runtime_state",
            "catalog",
            "cluster",
            "project",
            "icon",
            "project_url",
            "default_version",
            "catalog_name",
            "versions",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "catalog": {"lookup_field": "uuid", "view_name": "rancher-catalog-detail"},
            "cluster": {"lookup_field": "uuid", "view_name": "rancher-cluster-detail"},
            "project": {"lookup_field": "uuid", "view_name": "rancher-project-detail"},
        }


class RancherApplicationSerializer(structure_serializers.BaseResourceSerializer):
    version = serializers.CharField()
    namespace_name = serializers.CharField(required=False, write_only=True)
    answers = serializers.DictField(required=False)
    rancher_project_name = serializers.ReadOnlyField(source="rancher_project.name")
    catalog_name = serializers.ReadOnlyField(source="template.catalog.name")
    template_name = serializers.ReadOnlyField(source="template.name")

    class Meta:
        model = models.Application
        view_name = "rancher-app-detail"
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "runtime_state",
            "template",
            "rancher_project",
            "namespace",
            "namespace_name",
            "version",
            "answers",
            "rancher_project_name",
            "catalog_name",
            "template_name",
            "external_url",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "template": {
                "lookup_field": "uuid",
                "view_name": "rancher-template-detail",
            },
            "namespace": {
                "lookup_field": "uuid",
                "view_name": "rancher-namespace-detail",
                "required": False,
            },
            "rancher_project": {
                "lookup_field": "uuid",
                "view_name": "rancher-project-detail",
            },
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if (not attrs.get("namespace") and not attrs.get("namespace_name")) or (
            attrs.get("namespace") and attrs.get("namespace_name")
        ):
            raise serializers.ValidationError(
                _(
                    "Either existing namespace UUID or new namespace name should be specified."
                )
            )

        template = attrs["template"]
        rancher_project = attrs["rancher_project"]
        settings_set = {template.settings, rancher_project.settings}

        namespace = attrs.get("namespace")
        namespace_name = attrs.pop("namespace_name", None)
        if namespace:
            settings_set.add(namespace.settings)

            if namespace.project != rancher_project:
                raise serializers.ValidationError(
                    _("Namespace should belong to the same project.")
                )
        elif namespace_name:
            attrs["namespace"] = models.Namespace.objects.create(
                name=namespace_name,
                settings=rancher_project.settings,
                project=rancher_project,
            )
        else:
            raise serializers.ValidationError(_("Namespace is not specified."))

        if len(settings_set) > 1:
            raise serializers.ValidationError(
                _(
                    "The same settings should be used for template, project and namespace."
                )
            )

        return attrs

    def create(self, validated_data):
        rancher_project = cast(models.Project, validated_data["rancher_project"])
        validated_data["settings"] = rancher_project.settings
        if rancher_project.cluster:
            utils.check_managed_cluster(
                rancher_project.cluster, self.context["request"].user
            )
        validated_data["cluster"] = rancher_project.cluster
        return super().create(validated_data)


class RancherWorkloadSerializer(serializers.HyperlinkedModelSerializer):
    cluster_uuid = serializers.UUIDField(read_only=True, source="cluster.uuid")
    cluster_name = serializers.ReadOnlyField(source="cluster.name")
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.ReadOnlyField(source="project.name")
    namespace_uuid = serializers.UUIDField(read_only=True, source="namespace.uuid")
    namespace_name = serializers.ReadOnlyField(source="namespace.name")

    class Meta:
        model = models.Workload
        fields = (
            "url",
            "uuid",
            "name",
            "created",
            "modified",
            "runtime_state",
            "cluster",
            "cluster_uuid",
            "cluster_name",
            "project",
            "project_uuid",
            "project_name",
            "namespace",
            "namespace_uuid",
            "namespace_name",
            "scale",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "rancher-workload-detail"},
            "cluster": {"lookup_field": "uuid", "view_name": "rancher-cluster-detail"},
            "project": {"lookup_field": "uuid", "view_name": "rancher-project-detail"},
            "namespace": {
                "lookup_field": "uuid",
                "view_name": "rancher-namespace-detail",
            },
        }

    def create(self, validated_data):
        workload = cast(models.Workload, validated_data["workload"])
        if workload.cluster:
            utils.check_managed_cluster(workload.cluster, self.context["request"].user)
        return super().create(validated_data)


class RancherHPASerializer(serializers.HyperlinkedModelSerializer):
    cluster_uuid = serializers.UUIDField(read_only=True, source="cluster.uuid")
    cluster_name = serializers.ReadOnlyField(source="cluster.name")
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")
    project_name = serializers.ReadOnlyField(source="project.name")
    namespace_uuid = serializers.UUIDField(read_only=True, source="namespace.uuid")
    namespace_name = serializers.ReadOnlyField(source="namespace.name")
    workload_uuid = serializers.UUIDField(read_only=True, source="workload.uuid")
    workload_name = serializers.ReadOnlyField(source="workload.name")

    class Meta:
        model = models.HPA
        fields = (
            "url",
            "uuid",
            "name",
            "description",
            "created",
            "modified",
            "runtime_state",
            "cluster",
            "cluster_uuid",
            "cluster_name",
            "project",
            "project_uuid",
            "project_name",
            "namespace",
            "namespace_uuid",
            "namespace_name",
            "workload",
            "workload_uuid",
            "workload_name",
            "min_replicas",
            "max_replicas",
            "current_replicas",
            "desired_replicas",
            "metrics",
        )
        read_only_fields = (
            "state",
            "runtime_state",
            "current_replicas",
            "desired_replicas",
            "cluster",
            "project",
            "namespace",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "rancher-hpa-detail"},
            "cluster": {"lookup_field": "uuid", "view_name": "rancher-cluster-detail"},
            "project": {"lookup_field": "uuid", "view_name": "rancher-project-detail"},
            "namespace": {
                "lookup_field": "uuid",
                "view_name": "rancher-namespace-detail",
            },
            "workload": {
                "lookup_field": "uuid",
                "view_name": "rancher-workload-detail",
            },
        }

    def create(self, validated_data):
        workload = cast(models.Workload, validated_data["workload"])
        if workload.cluster:
            utils.check_managed_cluster(workload.cluster, self.context["request"].user)

        validated_data["settings"] = workload.settings
        validated_data["cluster"] = workload.cluster
        validated_data["project"] = workload.project
        validated_data["namespace"] = workload.namespace
        return super().create(validated_data)


class RancherConsoleLogSerializer(serializers.Serializer):
    length = serializers.IntegerField(required=False)


class RancherUserClusterLinkSerializer(serializers.HyperlinkedModelSerializer):
    cluster_name = serializers.ReadOnlyField(source="cluster.name")
    cluster_uuid = serializers.UUIDField(read_only=True, source="cluster.uuid")

    class Meta:
        model = models.RancherUserClusterLink
        fields = ("cluster", "role", "cluster_name", "cluster_uuid")
        extra_kwargs = {
            "cluster": {"lookup_field": "uuid", "view_name": "rancher-cluster-detail"},
            "role": {
                "lookup_field": "uuid",
                "view_name": "rancher-role-template-detail",
            },
        }


class RancherUserProjectLinkSerializer(serializers.HyperlinkedModelSerializer):
    project_name = serializers.ReadOnlyField(source="project.name")
    project_uuid = serializers.UUIDField(read_only=True, source="project.uuid")

    class Meta:
        model = models.RancherUserProjectLink
        fields = ("project", "role", "project_name", "project_uuid")
        extra_kwargs = {
            "project": {"lookup_field": "uuid", "view_name": "rancher-project-detail"},
            "role": {
                "lookup_field": "uuid",
                "view_name": "rancher-role-template-detail",
            },
        }


class RancherUserSerializer(serializers.HyperlinkedModelSerializer):
    cluster_roles = RancherUserClusterLinkSerializer(many=True, read_only=True)

    project_roles = RancherUserProjectLinkSerializer(many=True, read_only=True)

    user_name = serializers.ReadOnlyField(source="user.username")
    full_name = serializers.ReadOnlyField(source="user.full_name")

    def __init__(self, instance=None, *args, **kwargs):
        if instance:
            if isinstance(instance, list):
                request = kwargs.get("context", {}).get("request")
                if request:
                    cluster_uuid = request.GET.get("cluster_uuid")
                    for user in instance:
                        if cluster_uuid:
                            user.cluster_roles = user.rancheruserclusterlink_set.filter(
                                cluster__uuid=cluster_uuid
                            )
                            user.project_roles = user.rancheruserprojectlink_set.filter(
                                project__cluster__uuid=cluster_uuid
                            )
                        else:
                            user.cluster_roles = user.rancheruserclusterlink_set.all()
                            user.project_roles = user.rancheruserprojectlink_set.all()
            else:
                instance.cluster_roles = instance.rancheruserclusterlink_set.all()
                instance.project_roles = instance.rancheruserprojectlink_set.all()

        super().__init__(instance=instance, *args, **kwargs)

    class Meta:
        model = models.RancherUser
        fields = (
            "url",
            "uuid",
            "user",
            "cluster_roles",
            "project_roles",
            "settings",
            "is_active",
            "user_name",
            "full_name",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "rancher-user-detail"},
            "user": {"lookup_field": "uuid", "view_name": "user-detail"},
            "settings": {"lookup_field": "uuid"},
        }


class RancherClusterTemplateNodeSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.ClusterTemplateNode
        fields = (
            "min_vcpu",
            "min_ram",
            "system_volume_size",
            "preferred_volume_type",
            "role",
        )


class RancherClusterTemplateSerializer(serializers.HyperlinkedModelSerializer):
    nodes = RancherClusterTemplateNodeSerializer(many=True)

    class Meta:
        model = models.ClusterTemplate
        fields = (
            "uuid",
            "name",
            "description",
            "created",
            "modified",
            "nodes",
        )


class RancherIngressSerializer(structure_serializers.BaseResourceSerializer):
    rancher_project_name = serializers.ReadOnlyField(source="rancher_project.name")
    namespace_name = serializers.ReadOnlyField(source="namespace.name")

    class Meta:
        model = models.Ingress
        view_name = "rancher-ingress-detail"
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "runtime_state",
            "rancher_project",
            "rancher_project_name",
            "namespace",
            "namespace_name",
            "rules",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "namespace": {
                "lookup_field": "uuid",
                "view_name": "rancher-namespace-detail",
                "required": False,
            },
            "rancher_project": {
                "lookup_field": "uuid",
                "view_name": "rancher-project-detail",
            },
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        rancher_project = attrs["rancher_project"]
        namespace = attrs["namespace"]

        if namespace.project != rancher_project:
            raise serializers.ValidationError(
                _("Namespace should belong to the same project.")
            )

        return attrs

    def create(self, validated_data):
        rancher_project = cast(models.Project, validated_data["rancher_project"])
        validated_data["settings"] = rancher_project.settings
        validated_data["cluster"] = rancher_project.cluster
        return super().create(validated_data)


class RancherNestedWorkloadSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.Workload
        fields = ("uuid", "url", "name")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }


class RancherWorkloadCreateSerializer(serializers.Serializer):
    url = serializers.HyperlinkedRelatedField(
        queryset=models.Workload.objects.all(),
        lookup_field="uuid",
        view_name="rancher-workload-detail",
    )

    def to_internal_value(self, data):
        return super().to_internal_value(data)["url"]


class RancherServiceSerializer(structure_serializers.BaseResourceSerializer):
    namespace_name = serializers.ReadOnlyField(source="namespace.name")
    target_workloads = RancherNestedWorkloadSerializer(many=True)

    class Meta:
        model = models.Service
        view_name = "rancher-service-detail"
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "runtime_state",
            "namespace",
            "namespace_name",
            "cluster_ip",
            "selector",
            "target_workloads",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "namespace": {
                "lookup_field": "uuid",
                "view_name": "rancher-namespace-detail",
                "required": False,
            },
        }

    def create(self, validated_data):
        namespace = validated_data["namespace"]
        validated_data["settings"] = namespace.settings
        return super().create(validated_data)


class RancherServiceCreateSerializer(RancherServiceSerializer):
    target_workloads = RancherWorkloadCreateSerializer(many=True, required=False)


class RancherImportYamlSerializer(serializers.Serializer):
    yaml = serializers.CharField()
    default_namespace = serializers.HyperlinkedRelatedField(
        view_name="rancher-namespace-detail",
        lookup_field="uuid",
        queryset=models.Namespace.objects.all(),
        required=False,
        allow_null=True,
    )
    namespace = serializers.HyperlinkedRelatedField(
        view_name="rancher-namespace-detail",
        lookup_field="uuid",
        queryset=models.Namespace.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        fields = (
            "yaml",
            "default_namespace",
            "namespace",
        )

    def validate(self, attrs):
        cluster = self.context["view"].get_object()
        namespace = attrs.get("namespace")
        default_namespace = attrs.get("default_namespace")

        if namespace and namespace.project.cluster != cluster:
            raise serializers.ValidationError(
                _("Namespace should be related to the same cluster.")
            )

        if default_namespace and default_namespace.project.cluster != cluster:
            raise serializers.ValidationError(
                _("Default namespace should be related to the same cluster.")
            )

        return attrs


class RancherCreateManagementSecurityGroupSerializer(serializers.Serializer):
    cidr = serializers.CharField(
        default="192.168.42.0/24",
        initial="192.168.42.0/24",
    )
    ethertype = serializers.ChoiceField(
        choices=openstack_models.SecurityGroupRule.ETHER_TYPES,
        initial=openstack_models.SecurityGroupRule.IPv4,
        default=openstack_models.SecurityGroupRule.IPv4,
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        validators = {
            openstack_models.SecurityGroupRule.IPv4: is_valid_ipv4_cidr,
            openstack_models.SecurityGroupRule.IPv6: is_valid_ipv6_cidr,
        }
        validator = validators[attrs["ethertype"]]
        if not validator(attrs["cidr"]):
            raise serializers.ValidationError(
                _("Invalid CIDR format: %s") % attrs["cidr"]
            )
        return attrs


class RancherClusterReference(serializers.ModelSerializer):
    class Meta:
        model = models.Cluster
        fields = ("uuid", "name", "marketplace_uuid")


@extend_schema_field(RancherClusterReference(allow_null=True))
def get_rancher_cluster_for_openstack_instance(
    serializer, scope: openstack_models.Instance
):
    request = serializer.context["request"]
    queryset = filter_queryset_for_user(models.Cluster.objects.all(), request.user)
    try:
        if not models.Node.objects.filter(instance=scope).exists():
            return

        cluster = queryset.filter(tenant=scope.tenant).get()
    except (models.Cluster.DoesNotExist, MultipleObjectsReturned):
        return None
    return RancherClusterReference(cluster).data


def add_rancher_cluster_to_openstack_instance(sender, fields, **kwargs):
    """Add Rancher cluster information to OpenStack instance serializer."""
    fields["rancher_cluster"] = serializers.SerializerMethodField()
    setattr(sender, "get_rancher_cluster", get_rancher_cluster_for_openstack_instance)


core_signals.pre_serializer_fields.connect(
    sender=openstack_serializers.OpenStackInstanceSerializer,
    receiver=add_rancher_cluster_to_openstack_instance,
)


class RancherFieldPropsSerializer(serializers.Serializer):
    label = serializers.CharField()
    description = serializers.CharField(required=False)
    variable = serializers.CharField()
    required = serializers.BooleanField(required=False)
    validate_ = serializers.JSONField(required=False)


class RancherTemplateBaseQuestionSerializer(RancherFieldPropsSerializer):
    type = serializers.ChoiceField(choices=RANCHER_TEMPLATE_QUESTION_TYPE)
    default = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    group = serializers.CharField(required=False)
    showIf = serializers.CharField(required=False)


class RancherTemplateQuestionSerializer(RancherTemplateBaseQuestionSerializer):
    subquestions = RancherTemplateBaseQuestionSerializer(many=True, required=False)
    showSubquestionIf = serializers.CharField(required=False)


class TemplateVersionSerializer(serializers.Serializer):
    readme = serializers.CharField(read_only=True)
    app_readme = serializers.CharField(read_only=True)
    questions = RancherTemplateQuestionSerializer(many=True, read_only=True)


class RoleTemplateSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.RoleTemplate
        fields = (
            "url",
            "uuid",
            "name",
            "scope_type",
            "display_name",
            "settings",
        )
        read_only_fields = fields
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "rancher-role-template-detail",
            },
            "settings": {"lookup_field": "uuid"},
        }


class KeycloakGroupSerializer(serializers.HyperlinkedModelSerializer):
    scope_name = serializers.SerializerMethodField()
    scope_type = serializers.CharField(source="role.scope_type", read_only=True)

    class Meta:
        model = models.KeycloakGroup
        fields = (
            "uuid",
            "url",
            "name",
            "backend_id",
            "scope_type",
            "scope_uuid",
            "scope_name",
            "role",
            "created",
            "modified",
        )
        read_only_fields = ("uuid", "url", "created", "modified", "backend_id", "name")
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "keycloak-group-detail",
            },
            "role": {
                "lookup_field": "uuid",
                "view_name": "rancher-role-template-detail",
            },
        }

    def get_scope_name(self, obj: models.KeycloakGroup) -> str | None:
        """Get the name of the cluster or project"""
        scope_type = obj.role.scope_type
        scope_uuid = obj.scope_uuid
        if scope_type == RoleScopeType.CLUSTER:
            try:
                return models.Cluster.objects.get(uuid=scope_uuid).name
            except models.Cluster.DoesNotExist:
                return None
        elif scope_type == RoleScopeType.PROJECT:
            try:
                return models.Project.objects.get(uuid=scope_uuid).name
            except models.Project.DoesNotExist:
                return None
        return None


class KeycloakUserGroupMembershipSerializer(serializers.HyperlinkedModelSerializer):
    scope_uuid = serializers.UUIDField(
        help_text=_("UUID of a cluster or a project in Rancher"),
        write_only=True,
    )
    role = serializers.HyperlinkedRelatedField(
        view_name="rancher-role-template-detail",
        lookup_field="uuid",
        queryset=models.RoleTemplate.objects.all(),
        write_only=True,
    )
    group = serializers.HyperlinkedRelatedField(
        view_name="keycloak-group-detail",
        lookup_field="uuid",
        read_only=True,
    )
    group_name = serializers.CharField(source="group.name", read_only=True)
    group_role = serializers.CharField(source="group.role", read_only=True)
    group_scope_type = serializers.CharField(
        source="group.role.scope_type", read_only=True
    )
    group_scope_name = serializers.SerializerMethodField()

    class Meta:
        model = models.KeycloakUserGroupMembership
        fields = (
            "uuid",
            "url",
            "username",
            "email",
            "first_name",
            "last_name",
            "group",
            "group_name",
            "group_role",
            "group_scope_type",
            "group_scope_name",
            "scope_uuid",
            "role",
            "state",
            "created",
            "modified",
            "last_checked",
            "error_message",
            "error_traceback",
        )
        read_only_fields = (
            "uuid",
            "first_name",
            "last_name",
            "state",
            "created",
            "modified",
            "last_checked",
            "error_message",
            "error_traceback",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "keycloak-user-group-membership-detail",
            },
        }

    def validate(self, attrs):
        role = attrs.get("role")
        scope_uuid = attrs.get("scope_uuid")

        # Validate that the scope exists
        try:
            utils.get_keycloak_group_scope_and_settings(
                models.KeycloakGroup(role=role, scope_uuid=scope_uuid)
            )
        except models.Cluster.DoesNotExist:
            raise serializers.ValidationError(
                _("Cluster with UUID %s does not exist.") % scope_uuid
            )
        except models.Project.DoesNotExist:
            raise serializers.ValidationError(
                _("Project with UUID %s does not exist.") % scope_uuid
            )

        # Check if membership already exists
        if models.KeycloakUserGroupMembership.objects.filter(
            username=attrs["username"],
            group__role=role,
        ).exists():
            raise serializers.ValidationError(
                _("This keycloak user group membership already exists.")
            )

        return attrs

    def create(self, validated_data):
        scope_uuid = validated_data.pop("scope_uuid")
        role = validated_data.pop("role")
        group = models.KeycloakGroup.objects.filter(
            role=role, scope_uuid=scope_uuid
        ).first()
        validated_data["group"] = group
        return super().create(validated_data)

    def get_group_scope_name(
        self, obj: models.KeycloakUserGroupMembership
    ) -> str | None:
        """Get the name of the cluster or project"""
        try:
            scope, _ = utils.get_keycloak_group_scope_and_settings(obj.group)
            return scope.name
        except (models.Cluster.DoesNotExist, models.Project.DoesNotExist):
            return None


class RancherClusterSecurityGroupRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ClusterSecurityGroupRule
        fields = (
            "uuid",
            "ethertype",
            "direction",
            "protocol",
            "from_port",
            "to_port",
            "cidr",
            "description",
        )

    def validate(self, rule):
        validate_security_group_rule(self.to_representation(rule))
        return rule

    def to_internal_value(self, data):
        """Create new rule if uuid is not specified, update exist rule uuid is specified"""
        group: models.ClusterSecurityGroup = self.context["view"].get_object()
        internal_data = super().to_internal_value(data)
        if "uuid" not in data:
            return models.ClusterSecurityGroupRule(group=group, **internal_data)
        rule_uuid = data.pop("uuid")
        try:
            rule = models.ClusterSecurityGroupRule.objects.filter(group=group).get(
                id=rule_uuid
            )
        except models.ClusterSecurityGroupRule.DoesNotExist:
            raise serializers.ValidationError(
                {"uuid": _("Security group does not have rule with id %s.") % rule_uuid}
            )
        for key, value in internal_data.items():
            setattr(rule, key, value)
        return rule


class ClusterSecurityGroupSerializer(serializers.ModelSerializer):
    rules = RancherClusterSecurityGroupRuleSerializer(many=True)

    class Meta:
        model = models.ClusterSecurityGroup
        fields = (
            "uuid",
            "name",
            "description",
            "rules",
        )
        extra_kwargs = {
            "name": {"read_only": True},
            "description": {"read_only": True},
        }

    @transaction.atomic()
    def save(self, **kwargs):
        group: models.ClusterSecurityGroup = self.context["view"].get_object()
        rules: list[models.ClusterSecurityGroupRule] = self.validated_data["rules"]

        # Delete stale security group rules
        models.ClusterSecurityGroupRule.objects.filter(group=group).exclude(
            uuid__in=[rule.uuid for rule in rules if rule.uuid]
        ).delete()

        # Save new or updated security group rules
        for rule in rules:
            rule.save()
        return group


class SecretSerializer(serializers.Serializer):
    name = serializers.CharField()
    id = serializers.CharField()
