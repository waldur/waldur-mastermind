import functools
import logging
import operator
from typing import cast

from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from keycloak import exceptions as keycloak_exceptions
from rest_framework import decorators, generics, mixins, response, status, viewsets
from rest_framework import serializers as rf_serializers
from rest_framework.exceptions import MethodNotAllowed, ValidationError
from rest_framework.permissions import SAFE_METHODS

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.core.enums import CoreStates
from waldur_core.core.models import User
from waldur_core.core.serializers import DetailSerializer
from waldur_core.structure import exceptions as structure_exceptions
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.permissions import is_administrator
from waldur_core.structure.serializers import ConsoleUrlSerializer
from waldur_mastermind.common import utils as common_utils
from waldur_openstack import models as openstack_models
from waldur_openstack import views as openstack_views
from waldur_openstack.executors import PushSecurityGroupRulesExecutor
from waldur_rancher import (
    backend,
    exceptions,
    executors,
    filters,
    models,
    serializers,
    utils,
    validators,
)
from waldur_rancher.apps import RancherConfig
from waldur_rancher.enums import AGENT_ROLE, RoleScopeType
from waldur_rancher.exceptions import RancherException

logger = logging.getLogger(__name__)


class OptionalReadonlyViewset:
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not django_settings.WALDUR_RANCHER["READ_ONLY_MODE"]:
            return
        if self.action in ("import_resource", "pull") and request.user.is_staff:
            return
        if self.request.method not in SAFE_METHODS:
            raise MethodNotAllowed(method=request.method)


class ClusterViewSet(OptionalReadonlyViewset, structure_views.ResourceViewSet):
    queryset = models.Cluster.objects.all().order_by("name")
    serializer_class = serializers.RancherClusterSerializer
    filterset_class = filters.ClusterFilter
    update_executor = executors.ClusterUpdateExecutor
    disabled_actions = ["create", "destroy"]

    update_validators = partial_update_validators = [
        core_validators.StateValidator(CoreStates.OK),
    ]
    pull_executor = executors.ClusterPullExecutor

    @extend_schema(responses={status.HTTP_200_OK: None})
    @decorators.action(detail=True, methods=["post"])
    def import_yaml(self, request, uuid=None):
        cluster: models.Cluster = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        yaml = serializer.validated_data["yaml"]
        default_namespace = serializer.validated_data.get("default_namespace")
        namespace = serializer.validated_data.get("namespace")

        backend = cluster.get_backend()
        try:
            backend.import_yaml(
                cluster, yaml, default_namespace=default_namespace, namespace=namespace
            )
        except exceptions.RancherException as e:
            message = e.args[0].get("message", "Server error")
            return response.Response(
                {"details": message}, status=status.HTTP_400_BAD_REQUEST
            )

        executors.ClusterPullExecutor.execute(cluster)

        return response.Response(status.HTTP_200_OK)

    import_yaml_serializer_class = serializers.RancherImportYamlSerializer
    import_yaml_permissions = [structure_permissions.is_staff]

    @extend_schema(
        responses={
            status.HTTP_201_CREATED: inline_serializer(
                "RancherCreateManagementSecurityGroupResponse",
                fields={"security_group_uuid": rf_serializers.CharField()},
            )
        }
    )
    @decorators.action(detail=True, methods=["post"])
    def create_management_security_group(self, request, uuid=None):
        serializer = serializers.RancherCreateManagementSecurityGroupSerializer(
            data=request.data, many=True
        )
        serializer.is_valid(raise_exception=True)
        cluster: models.Cluster = self.get_object()
        user = request.user
        tenant = utils.get_management_tenant(cluster)
        port = cluster.settings.get_option("management_tenant_access_port")

        rules = []

        for rule in serializer.validated_data:
            rules.append(
                {
                    "protocol": "tcp",
                    "from_port": port,
                    "to_port": port,
                    "direction": openstack_models.SecurityGroupRule.INGRESS,
                    "ethertype": rule["ethertype"],
                    "cidr": rule["cidr"],
                }
            )

        post_data = {
            "name": cluster.name,
            "description": "Access for management of cluster %s" % cluster.name,
            "rules": rules,
        }
        view = openstack_views.TenantViewSet.as_view({"post": "create_security_group"})
        group_response = common_utils.create_request(
            view, user, post_data, uuid=tenant.uuid.hex
        )

        if group_response.status_code != status.HTTP_201_CREATED:
            return response.Response(
                group_response.data, status=group_response.status_code
            )

        security_group = openstack_models.SecurityGroup.objects.get(
            uuid=group_response.data.get("uuid")
        )
        cluster.management_security_group = security_group
        cluster.save()
        return response.Response(
            {"security_group_uuid": security_group.uuid.hex},
            status=status.HTTP_201_CREATED,
        )

    create_management_security_group_validators = (
        validators.creation_of_management_security_group_is_available,
        core_validators.StateValidator(CoreStates.OK),
    )


class NodeViewSet(OptionalReadonlyViewset, structure_views.ResourceViewSet):
    queryset = models.Node.objects.all()
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    serializer_class = serializers.RancherNodeSerializer
    create_serializer_class = serializers.RancherCreateNodeSerializer
    filterset_class = filters.NodeFilter
    lookup_field = "uuid"
    disabled_actions = ["update", "partial_update"]
    create_permissions = [structure_permissions.is_staff]
    destroy_validators = []
    pull_executor = executors.NodePullExecutor

    def perform_create(self, serializer):
        node: models.Node = serializer.save()
        user = self.request.user
        transaction.on_commit(
            lambda: executors.NodeCreateExecutor.execute(
                node,
                user_id=user.id,
                is_heavy_task=True,
            )
        )

    def destroy(self, request, *args, **kwargs):
        instance: models.Node = self.get_object()
        if (
            instance.role == AGENT_ROLE
            and instance.cluster.node_set.filter(role=AGENT_ROLE).count() == 1
        ):
            # Prevent deletion of the last agent node in the cluster
            raise ValidationError(
                _("Cannot delete the last agent node in the cluster.")
            )
        user = self.request.user
        executors.NodeDeleteExecutor.execute(
            instance,
            user_id=user.id,
            is_heavy_task=True,
        )
        return response.Response(status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        request=serializers.LinkOpenstackSerializer,
        responses=None,
        description="Links node to OpenStack instance.",
    )
    @decorators.action(detail=True, methods=["post"])
    def link_openstack(self, request, uuid=None):
        node: models.Node = self.get_object()

        if node.instance:
            raise ValidationError(_("Node is already linked to OpenStack instance."))

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.validated_data["instance"]
        if models.Node.objects.filter(instance=instance).exists():
            raise ValidationError(
                _("OpenStack instance is already linked to another node.")
            )

        node.instance = instance
        node.save()
        return response.Response(status=status.HTTP_200_OK)

    link_openstack_permissions = [structure_permissions.is_staff]
    link_openstack_serializer_class = serializers.LinkOpenstackSerializer

    @extend_schema(
        request=None,
        responses=None,
        description="Unlinks node from OpenStack instance.",
    )
    @decorators.action(detail=True, methods=["post"])
    def unlink_openstack(self, request, uuid=None):
        node: models.Node = self.get_object()
        if not node.instance:
            raise ValidationError(
                _("Node is not linked to any OpenStack instance yet.")
            )
        node.instance = None
        node.save()
        return response.Response(status=status.HTTP_200_OK)

    unlink_openstack_permissions = [structure_permissions.is_staff]

    @extend_schema(
        description="Returns console URL for the node.",
        responses=ConsoleUrlSerializer,
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def console(self, request, uuid=None):
        node: models.Node = self.get_object()

        if not node.instance:
            return response.Response(status=status.HTTP_404_NOT_FOUND)

        backend = node.instance.get_backend()
        backend_method = getattr(backend, "get_console_url")

        if backend_method:
            try:
                url = backend_method(node.instance)
            except structure_exceptions.SerializableBackendError as e:
                raise ValidationError(str(e))

            return response.Response({"url": url}, status=status.HTTP_200_OK)
        else:
            return response.Response(status=status.HTTP_404_NOT_FOUND)

    console_validators = [validators.console_validator]
    console_permissions = [utils.check_permissions_for_console()]

    @extend_schema(
        description="Returns console log for the node.",
        responses={200: str, 404: None},
        parameters=[OpenApiParameter("length", int, OpenApiParameter.QUERY)],
        filters=False,
    )
    @decorators.action(detail=True, methods=["get"])
    def console_log(self, request, uuid=None):
        node: models.Node = self.get_object()

        if not node.instance:
            return response.Response(status=status.HTTP_404_NOT_FOUND)

        backend = node.instance.get_backend()
        serializer = self.get_serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        length = serializer.validated_data.get("length")
        backend_method = getattr(backend, "get_console_output")

        if backend_method:
            try:
                log = backend_method(node.instance, length)
            except structure_exceptions.SerializableBackendError as e:
                raise ValidationError(str(e))

            return response.Response(log, status=status.HTTP_200_OK)
        else:
            return response.Response(status=status.HTTP_404_NOT_FOUND)

    console_log_serializer_class = serializers.RancherConsoleLogSerializer
    console_log_permissions = [utils.check_permissions_for_console_log()]


class CatalogViewSet(OptionalReadonlyViewset, core_views.ActionsViewSet):
    queryset = models.Catalog.objects.all()
    serializer_class = serializers.RancherCatalogSerializer
    lookup_field = "uuid"

    def get_queryset(self):
        settings_uuid = self.request.query_params.get("settings_uuid")
        cluster_uuid = self.request.query_params.get("cluster_uuid")
        if settings_uuid:
            return self.filter_catalogs_for_settings(settings_uuid)
        elif cluster_uuid:
            return self.filter_catalogs_for_cluster(cluster_uuid)
        else:
            return self.filter_visible_catalogs()

    def filter_catalogs_for_settings(self, settings_uuid):
        qs = ServiceSettings.objects.filter(type=RancherConfig.service_name)
        scope = get_object_or_404(qs, uuid=settings_uuid)
        ctype = ContentType.objects.get_for_model(ServiceSettings)
        return self.queryset.filter(content_type=ctype, object_id=scope.id)

    def filter_catalogs_for_cluster(self, cluster_uuid):
        qs = filter_queryset_for_user(
            queryset=models.Cluster.objects.all(),
            user=self.request.user,
        )
        cluster = get_object_or_404(qs, uuid=cluster_uuid)
        return self.queryset.filter(
            Q(
                content_type=ContentType.objects.get_for_model(models.Cluster),
                object_id=cluster.id,
            )
            | Q(
                content_type=ContentType.objects.get_for_model(ServiceSettings),
                object_id=cluster.service_settings.id,
            )
        )

    def filter_visible_catalogs(self):
        settings_subquery = self.get_filtered_subquery(
            models.ServiceSettings.objects.filter(type=RancherConfig.service_name)
        )
        clusters_subquery = self.get_filtered_subquery(models.Cluster.objects.all())
        projects_subquery = self.get_filtered_subquery(models.Project.objects.all())
        subqueries = [settings_subquery, clusters_subquery, projects_subquery]
        subqueries = [query for query in subqueries if query]
        if subqueries:
            visible_scopes = functools.reduce(operator.or_, subqueries)
            return self.queryset.filter(visible_scopes)
        return self.queryset.none()

    def get_filtered_subquery(self, queryset):
        ids = filter_queryset_for_user(
            queryset=queryset,
            user=self.request.user,
        ).values_list("id", flat=True)
        content_type = ContentType.objects.get_for_model(queryset.model)
        if not ids:
            return
        return functools.reduce(
            operator.or_,
            [Q(content_type=content_type, object_id=object_id) for object_id in ids],
        )

    @extend_schema(request=None, responses={status.HTTP_200_OK: None})
    @decorators.action(detail=True, methods=["post"])
    def refresh(self, request, uuid=None):
        catalog: models.Catalog = self.get_object()
        backend = catalog.get_backend()
        backend.refresh_catalog(catalog)
        return response.Response(status=status.HTTP_200_OK)

    refresh_permissions = [structure_permissions.is_staff]

    def perform_create(self, serializer):
        scope = serializer.validated_data["scope"]
        self.check_catalog_permissions(scope)

        if isinstance(scope, ServiceSettings):
            service_settings = scope
            if scope.type != RancherConfig.service_name:
                raise ValidationError(_("Invalid provider detected."))
        elif isinstance(scope, models.Cluster | models.Project):
            service_settings = scope.settings
        else:
            raise ValidationError(_("Invalid scope provided."))

        catalog: models.Catalog = serializer.save(settings=service_settings)
        backend = catalog.get_backend()
        backend.create_catalog(catalog)

    create_serializer_class = serializers.RancherCatalogCreateSerializer

    def perform_update(self, serializer):
        scope = serializer.instance.scope
        self.check_catalog_permissions(scope)
        catalog: models.Catalog = serializer.save()
        backend = catalog.get_backend()
        backend.update_catalog(catalog)

    update_serializer_class = serializers.RancherCatalogUpdateSerializer

    def perform_destroy(self, catalog):
        self.check_catalog_permissions(catalog.scope)
        backend = catalog.get_backend()
        backend.delete_catalog(catalog)
        catalog.delete()

    def check_catalog_permissions(self, scope):
        user = cast(User, self.request.user)
        if isinstance(scope, ServiceSettings) and not user:
            raise ValidationError(_("Only staff is allowed to manage global catalogs."))
        if isinstance(scope, models.Cluster):
            is_administrator(self.request, scope.project)
            utils.check_managed_cluster(scope, user)


class ProjectViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Project.objects.all().order_by("name")
    serializer_class = serializers.RancherProjectSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ProjectFilter
    lookup_field = "uuid"

    @extend_schema(
        filters=False,
        description="Returns project's secrets.",
        responses=serializers.SecretSerializer(many=True),
    )
    @decorators.action(detail=True, methods=["get"])
    def secrets(self, request, uuid=None):
        project: models.Project = self.get_object()
        backend = project.get_backend()
        secrets = backend.list_project_secrets(project)
        data = [{"name": secret["name"], "id": secret["id"]} for secret in secrets]
        return response.Response(data, status=status.HTTP_200_OK)


class NamespaceViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Namespace.objects.exclude(project__name="System").order_by("name")
    serializer_class = serializers.RancherNamespaceSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.NamespaceFilter
    lookup_field = "uuid"


class TemplateViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Template.objects.exclude(project__name="System")
    serializer_class = serializers.RancherTemplateSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.TemplateFilter
    lookup_field = "uuid"


class TemplateVersionView(generics.GenericAPIView):
    filter_backends = []
    serializer_class = serializers.TemplateVersionSerializer

    def get(self, request, template_uuid, version):
        queryset = models.Template.objects.all()
        queryset = filter_queryset_for_user(queryset, request.user)
        template = get_object_or_404(queryset, uuid=template_uuid)
        client = template.settings.get_backend().client
        details = client.get_template_version_details(template.backend_id, version)
        readme = client.get_template_version_readme(template.backend_id, version)
        app_readme = client.get_template_version_app_readme(
            template.backend_id, version
        )
        return response.Response(
            {
                "questions": details.get("questions"),
                "readme": readme,
                "app_readme": app_readme,
            }
        )


class ApplicationViewSet(OptionalReadonlyViewset, structure_views.ResourceViewSet):
    queryset = models.Application.objects.exclude(
        rancher_project__name="System"
    ).order_by("name")
    serializer_class = serializers.RancherApplicationSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ApplicationFilter
    lookup_field = "uuid"
    create_executor = executors.ApplicationCreateExecutor
    delete_executor = executors.ApplicationDeleteExecutor
    unsafe_methods_permissions = [
        is_administrator,
        utils.check_managed_cluster_permission,
    ]


class UserViewSet(core_views.ReadOnlyActionsViewSet):
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    queryset = models.RancherUser.objects.all()
    serializer_class = serializers.RancherUserSerializer
    filterset_class = filters.UserFilter
    lookup_field = "uuid"


class YamlMixin:
    get_yaml_method = NotImplemented
    put_yaml_method = NotImplemented

    @extend_schema(responses={status.HTTP_200_OK: DetailSerializer})
    @decorators.action(detail=True, methods=["get", "put"])
    def yaml(self, request, *args, **kwargs):
        workload = self.get_object()
        backend = workload.get_backend()
        if request.method == "GET":
            yaml = getattr(backend, self.get_yaml_method)(workload)
            return response.Response({"yaml": yaml}, status=status.HTTP_200_OK)
        else:
            yaml = request.data["yaml"]
            try:
                getattr(backend, self.put_yaml_method)(workload, yaml)
            except RancherException as e:
                message = e.args[0].get("message", "Server error")
                return response.Response(
                    {"details": message}, status=status.HTTP_400_BAD_REQUEST
                )
            else:
                return response.Response(status=status.HTTP_200_OK)


class SyncDestroyMixin:
    delete_scope_method = NotImplemented

    def destroy(self, request, *args, **kwargs):
        scope = self.get_object()
        backend = scope.get_backend()
        method = getattr(backend, self.delete_scope_method)
        method(scope)
        scope.delete()
        return response.Response(status=status.HTTP_204_NO_CONTENT)


class WorkloadViewSet(
    OptionalReadonlyViewset, YamlMixin, SyncDestroyMixin, core_views.ActionsViewSet
):
    queryset = models.Workload.objects.exclude(project__name="System")
    serializer_class = serializers.RancherWorkloadSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.WorkloadFilter
    lookup_field = "uuid"
    get_yaml_method = "get_workload_yaml"
    put_yaml_method = "put_workload_yaml"
    delete_scope_method = "delete_workload"
    unsafe_methods_permissions = [
        is_administrator,
        utils.check_managed_cluster_permission,
    ]

    @extend_schema(request=None, responses=None)
    @decorators.action(detail=True, methods=["post"])
    def redeploy(self, request, *args, **kwargs):
        workload: models.Workload = self.get_object()
        backend = workload.get_backend()
        backend.redeploy_workload(workload)
        return response.Response(status=status.HTTP_200_OK)


class HPAViewSet(OptionalReadonlyViewset, YamlMixin, structure_views.ResourceViewSet):
    queryset = models.HPA.objects.exclude(project__name="System")
    serializer_class = serializers.RancherHPASerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.HPAFilter
    lookup_field = "uuid"
    create_executor = executors.HPACreateExecutor
    update_executor = executors.HPAUpdateExecutor
    delete_executor = executors.HPADeleteExecutor
    get_yaml_method = "get_hpa_yaml"
    put_yaml_method = "put_hpa_yaml"
    unsafe_methods_permissions = [
        is_administrator,
        utils.check_managed_cluster_permission,
    ]


class ClusterTemplateViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.ClusterTemplate.objects.all()
    serializer_class = serializers.RancherClusterTemplateSerializer
    lookup_field = "uuid"


class IngressViewSet(
    OptionalReadonlyViewset,
    YamlMixin,
    SyncDestroyMixin,
    structure_views.ResourceViewSet,
):
    queryset = models.Ingress.objects.exclude(rancher_project__name="System").order_by(
        "name"
    )
    serializer_class = serializers.RancherIngressSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.IngressFilter
    lookup_field = "uuid"
    get_yaml_method = "get_ingress_yaml"
    put_yaml_method = "put_ingress_yaml"
    delete_scope_method = "delete_ingress"


class ServiceViewSet(
    OptionalReadonlyViewset,
    YamlMixin,
    SyncDestroyMixin,
    structure_views.ResourceViewSet,
):
    queryset = models.Service.objects.all().order_by("name")
    serializer_class = serializers.RancherServiceSerializer
    create_serializer_class = serializers.RancherServiceCreateSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ServiceFilter
    lookup_field = "uuid"
    get_yaml_method = "get_service_yaml"
    put_yaml_method = "put_service_yaml"
    delete_scope_method = "delete_service"


class RoleTemplateViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.RoleTemplate.objects.all().order_by("name")
    serializer_class = serializers.RoleTemplateSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.RoleTemplateFilter
    lookup_field = "uuid"


class KeycloakGroupViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.KeycloakGroup.objects.all().order_by("-created")
    serializer_class = serializers.KeycloakGroupSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.KeycloakGroupFilter
    lookup_field = "uuid"

    def get_queryset(self):
        queryset = super().get_queryset()

        # Filter by user permissions
        user = self.request.user

        if not user.is_staff:
            # Get clusters and projects where user can manage users
            cluster_uuids = filter_queryset_for_user(
                models.Cluster.objects.all(), user
            ).values_list("uuid", flat=True)

            project_uuids = filter_queryset_for_user(
                models.Project.objects.all(), user
            ).values_list("uuid", flat=True)

            # Filter assignments based on permissions
            return queryset.filter(
                Q(
                    role__scope_type=RoleScopeType.CLUSTER,
                    scope_uuid__in=cluster_uuids,
                )
                | Q(
                    role__scope_type=RoleScopeType.PROJECT,
                    scope_uuid__in=project_uuids,
                )
            )

        return queryset


class KeycloakUserGroupMembershipViewSet(core_views.ActionsViewSet):
    queryset = models.KeycloakUserGroupMembership.objects.all().order_by("-created")
    serializer_class = serializers.KeycloakUserGroupMembershipSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.KeycloakUserGroupMembershipFilter
    lookup_field = "uuid"

    def get_queryset(self):
        queryset = super().get_queryset()

        # Filter by user permissions
        user = self.request.user

        if not user.is_staff:
            # Get clusters and projects where user can manage users
            cluster_uuids = filter_queryset_for_user(
                models.Cluster.objects.all(), user
            ).values_list("uuid", flat=True)

            project_uuids = filter_queryset_for_user(
                models.Project.objects.all(), user
            ).values_list("uuid", flat=True)

            # Filter assignments based on permissions
            return queryset.filter(
                Q(
                    group__role__scope_type=RoleScopeType.CLUSTER,
                    group__scope_uuid__in=cluster_uuids,
                )
                | Q(
                    group__role__scope_type=RoleScopeType.PROJECT,
                    group__scope_uuid__in=project_uuids,
                )
            )

        return queryset

    def perform_create(self, serializer):
        def create_keycloak_group(
            keycloak: backend.KeycloakBackend,
            scope,
            scope_type: str,
            role: models.RoleTemplate,
        ):
            # Create the parent group for the cluster
            if scope_type == RoleScopeType.CLUSTER:
                parent_group_name = f"c_{scope.uuid.hex}"
            else:
                parent_group_name = f"c_{scope.cluster.uuid.hex}"
            parent_group = keycloak.create_group(parent_group_name)
            # Optionally create the child group
            group = models.KeycloakGroup.objects.filter(
                role=role,
                scope_uuid=scope_uuid,
            ).first()
            if not group:
                # Create group if does not exist
                group_name_prefix = scope_type[0].lower()
                group_name = f"{group_name_prefix}_{scope.uuid.hex}_{role.name}"
                backend_group = keycloak.create_group(
                    group_name, parent_id=parent_group["id"]
                )
                group = models.KeycloakGroup(
                    name=group_name,
                    role=role,
                    scope_uuid=scope_uuid,
                )
                group.backend_id = backend_group["id"]
                group.save()

        scope_uuid = serializer.validated_data["scope_uuid"]
        role = serializer.validated_data["role"]
        scope_type = role.scope_type
        scope, settings = utils.get_keycloak_group_scope_and_settings(
            models.KeycloakGroup(
                role=role,
                scope_uuid=scope_uuid,
            )
        )

        try:
            keycloak = backend.KeycloakBackend(settings)
            create_keycloak_group(keycloak, scope, scope_type, role)
            # Create a user membership
            user_membership = serializer.save()
            backend_user = keycloak.find_user_by_username(user_membership.username)
            if backend_user is None:
                # The user might not exist in Keycloak yet
                logger.info(
                    "The user %s does not exist in Keycloak yet, skipping adding user to the group %s (%s)",
                    user_membership.username,
                    user_membership.group.name,
                    user_membership.group.backend_id,
                )
            else:
                keycloak.add_user_to_group(
                    backend_user["id"], user_membership.group.backend_id
                )
                user_membership.first_name = backend_user.get("firstName", "")
                user_membership.last_name = backend_user.get("lastName", "")
                user_membership.activate()
                user_membership.save()
            sync_frequency = settings.get_option("keycloak_sync_frequency") or 15
            rancher_url = settings.backend_url

            # Send notification email
            utils.send_user_membership_notification_email(
                user_membership,
                scope,
                rancher_url,
                sync_frequency,
            )
        except keycloak_exceptions.KeycloakError as e:
            raise ValidationError(f"Unable to add a user to the Keycloak group: {e}")


CLUSTER_UUID = OpenApiParameter(
    name="cluster_uuid",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
)


class RancherClusterSecurityGroupsViewSet(
    OptionalReadonlyViewset,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = serializers.ClusterSecurityGroupSerializer
    queryset = models.ClusterSecurityGroup.objects.all().order_by("name")
    filter_backends = (DjangoFilterBackend, structure_filters.GenericRoleFilter)
    filterset_class = filters.ClusterSecurityGroupFilter
    lookup_field = "uuid"

    def perform_update(self, serializer):
        cluster_security_group: models.ClusterSecurityGroup = serializer.save()
        tenant_ids = cluster_security_group.cluster.linked_tenant_ids
        for tenant_id in tenant_ids:
            # TODO: name of security group should be unique and immutable
            try:
                os_security_group = openstack_models.SecurityGroup.objects.get(
                    name=cluster_security_group.name,
                    tenant_id=tenant_id,
                )
            except openstack_models.SecurityGroup.DoesNotExist:
                raise ValidationError(
                    f"Security group {cluster_security_group.name} not found in tenant"
                )
            transaction.on_commit(
                lambda: PushSecurityGroupRulesExecutor().execute(os_security_group)
            )
