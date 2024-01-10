import functools
import logging
import operator

from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import decorators, response, status
from rest_framework.exceptions import MethodNotAllowed, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.views import APIView

from waldur_core.core import validators as core_validators
from waldur_core.core import views as core_views
from waldur_core.structure import exceptions as structure_exceptions
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import views as structure_views
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.permissions import is_administrator
from waldur_mastermind.common import utils as common_utils
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack import views as openstack_views
from waldur_rancher import (
    exceptions,
    executors,
    filters,
    models,
    serializers,
    utils,
    validators,
)
from waldur_rancher.apps import RancherConfig
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
    serializer_class = serializers.ClusterSerializer
    filterset_class = filters.ClusterFilter
    update_executor = executors.ClusterUpdateExecutor

    def perform_create(self, serializer):
        cluster = serializer.save()
        user = self.request.user
        nodes = serializer.validated_data.get("node_set")
        install_longhorn = serializer.validated_data["install_longhorn"]

        for node_data in nodes:
            node_data["cluster"] = cluster
            models.Node.objects.create(**node_data)

        transaction.on_commit(
            lambda: executors.ClusterCreateExecutor.execute(
                cluster,
                user=user,
                install_longhorn=install_longhorn,
                is_heavy_task=True,
            )
        )

    def destroy(self, request, *args, **kwargs):
        user = self.request.user
        instance = self.get_object()
        executors.ClusterDeleteExecutor.execute(
            instance,
            user=user,
            is_heavy_task=True,
        )
        return response.Response(
            {"detail": _("Deletion was scheduled.")}, status=status.HTTP_202_ACCEPTED
        )

    update_validators = partial_update_validators = [
        core_validators.StateValidator(models.Cluster.States.OK),
    ]
    destroy_validators = structure_views.ResourceViewSet.destroy_validators + [
        validators.all_cluster_related_vms_can_be_deleted,
    ]
    pull_executor = executors.ClusterPullExecutor

    @decorators.action(detail=True, methods=["get"])
    def kubeconfig_file(self, request, uuid=None):
        cluster = self.get_object()
        backend = cluster.get_backend()
        try:
            config = backend.get_kubeconfig_file(cluster)
        except exceptions.RancherException:
            raise ValidationError("Unable to get kubeconfig file.")

        return response.Response({"config": config}, status=status.HTTP_200_OK)

    kubeconfig_file_validators = [
        core_validators.StateValidator(models.Cluster.States.OK)
    ]
    kubeconfig_file_permissions = [structure_permissions.is_staff]

    @decorators.action(detail=True, methods=["post"])
    def import_yaml(self, request, uuid=None):
        cluster = self.get_object()
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

    import_yaml_serializer_class = serializers.ImportYamlSerializer

    @decorators.action(detail=True, methods=["post"])
    def create_management_security_group(self, request, uuid=None):
        serializer = serializers.CreateManagementSecurityGroupSerializer(
            data=request.data, many=True
        )
        serializer.is_valid(raise_exception=True)
        cluster = self.get_object()
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
        core_validators.StateValidator(models.Cluster.States.OK),
    )


class NodeViewSet(OptionalReadonlyViewset, structure_views.ResourceViewSet):
    queryset = models.Node.objects.all()
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    serializer_class = serializers.NodeSerializer
    create_serializer_class = serializers.CreateNodeSerializer
    filterset_class = filters.NodeFilter
    lookup_field = "uuid"
    disabled_actions = ["update", "partial_update"]
    create_permissions = [structure_permissions.is_staff]
    destroy_validators = [
        validators.related_vm_can_be_deleted,
    ]
    pull_executor = executors.NodePullExecutor

    def perform_create(self, serializer):
        node = serializer.save()
        user = self.request.user
        transaction.on_commit(
            lambda: executors.NodeCreateExecutor.execute(
                node,
                user_id=user.id,
                is_heavy_task=True,
            )
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        user = self.request.user
        executors.NodeDeleteExecutor.execute(
            instance,
            user_id=user.id,
            is_heavy_task=True,
        )
        return response.Response(status=status.HTTP_202_ACCEPTED)

    @decorators.action(detail=True, methods=["post"])
    def link_openstack(self, request, uuid=None):
        node = self.get_object()

        if node.content_type and node.object_id:
            raise ValidationError(_("Node is already linked to OpenStack instance."))

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.validated_data["instance"]
        instance_type = ContentType.objects.get_for_model(instance)

        if models.Node.objects.filter(
            content_type=instance_type, object_id=instance.id
        ).exists():
            raise ValidationError(
                _("OpenStack instance is already linked to another node.")
            )

        node.content_type = instance_type
        node.object_id = instance.id
        node.save()
        return response.Response(status=status.HTTP_200_OK)

    link_openstack_permissions = [structure_permissions.is_staff]
    link_openstack_serializer_class = serializers.LinkOpenstackSerializer

    @decorators.action(detail=True, methods=["post"])
    def unlink_openstack(self, request, uuid=None):
        node = self.get_object()
        if not node.content_type or not node.object_id:
            raise ValidationError(
                _("Node is not linked to any OpenStack instance yet.")
            )
        node.content_type = None
        node.object_id = None
        node.save()
        return response.Response(status=status.HTTP_200_OK)

    unlink_openstack_permissions = [structure_permissions.is_staff]

    @decorators.action(detail=True, methods=["get"])
    def console(self, request, uuid=None):
        node = self.get_object()

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

    @decorators.action(detail=True, methods=["get"])
    def console_log(self, request, uuid=None):
        node = self.get_object()

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

    console_log_serializer_class = serializers.ConsoleLogSerializer
    console_log_permissions = [utils.check_permissions_for_console_log()]


class CatalogViewSet(OptionalReadonlyViewset, core_views.ActionsViewSet):
    queryset = models.Catalog.objects.all()
    serializer_class = serializers.CatalogSerializer
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

    @decorators.action(detail=True, methods=["post"])
    def refresh(self, request, uuid=None):
        catalog = self.get_object()
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

        catalog = serializer.save(settings=service_settings)
        backend = catalog.get_backend()
        backend.create_catalog(catalog)

    create_serializer_class = serializers.CatalogCreateSerializer

    def perform_update(self, serializer):
        scope = serializer.instance.scope
        self.check_catalog_permissions(scope)
        catalog = serializer.save()
        backend = catalog.get_backend()
        backend.update_catalog(catalog)

    update_serializer_class = serializers.CatalogUpdateSerializer

    def perform_destroy(self, catalog):
        self.check_catalog_permissions(catalog.scope)
        backend = catalog.get_backend()
        backend.delete_catalog(catalog)
        catalog.delete()

    def check_catalog_permissions(self, scope):
        if isinstance(scope, ServiceSettings) and not self.request.user.is_staff:
            raise ValidationError(_("Only staff is allowed to manage global catalogs."))
        if isinstance(scope, models.Cluster):
            is_administrator(self.request.user, scope.project)


class ProjectViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Project.objects.all().order_by("name")
    serializer_class = serializers.ProjectSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ProjectFilter
    lookup_field = "uuid"

    @decorators.action(detail=True, methods=["get"])
    def secrets(self, request, uuid=None):
        project = self.get_object()
        backend = project.get_backend()
        secrets = backend.list_project_secrets(project)
        data = [{"name": secret["name"], "id": secret["id"]} for secret in secrets]
        return response.Response(data, status=status.HTTP_200_OK)


class NamespaceViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Namespace.objects.all().order_by("name")
    serializer_class = serializers.NamespaceSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.NamespaceFilter
    lookup_field = "uuid"


class TemplateViewSet(structure_views.BaseServicePropertyViewSet):
    queryset = models.Template.objects.all()
    serializer_class = serializers.TemplateSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.TemplateFilter
    lookup_field = "uuid"


class TemplateVersionView(APIView):
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
    queryset = models.Application.objects.all().order_by("name")
    serializer_class = serializers.ApplicationSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ApplicationFilter
    lookup_field = "uuid"
    create_executor = executors.ApplicationCreateExecutor
    delete_executor = executors.ApplicationDeleteExecutor


class UserViewSet(core_views.ReadOnlyActionsViewSet):
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    queryset = models.RancherUser.objects.all()
    serializer_class = serializers.RancherUserSerializer
    filterset_class = filters.UserFilter
    lookup_field = "uuid"


class YamlMixin:
    get_yaml_method = NotImplemented
    put_yaml_method = NotImplemented

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
    queryset = models.Workload.objects.all()
    serializer_class = serializers.WorkloadSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.WorkloadFilter
    lookup_field = "uuid"
    get_yaml_method = "get_workload_yaml"
    put_yaml_method = "put_workload_yaml"
    delete_scope_method = "delete_workload"

    @decorators.action(detail=True, methods=["post"])
    def redeploy(self, request, *args, **kwargs):
        workload = self.get_object()
        backend = workload.get_backend()
        backend.redeploy_workload(workload)
        return response.Response(status=status.HTTP_200_OK)


class HPAViewSet(OptionalReadonlyViewset, YamlMixin, structure_views.ResourceViewSet):
    queryset = models.HPA.objects.all()
    serializer_class = serializers.HPASerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.HPAFilter
    lookup_field = "uuid"
    create_executor = executors.HPACreateExecutor
    update_executor = executors.HPAUpdateExecutor
    delete_executor = executors.HPADeleteExecutor
    get_yaml_method = "get_hpa_yaml"
    put_yaml_method = "put_hpa_yaml"


class ClusterTemplateViewSet(core_views.ReadOnlyActionsViewSet):
    queryset = models.ClusterTemplate.objects.all()
    serializer_class = serializers.ClusterTemplateSerializer
    lookup_field = "uuid"


class IngressViewSet(
    OptionalReadonlyViewset,
    YamlMixin,
    SyncDestroyMixin,
    structure_views.ResourceViewSet,
):
    queryset = models.Ingress.objects.all().order_by("name")
    serializer_class = serializers.IngressSerializer
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
    serializer_class = serializers.ServiceSerializer
    filter_backends = (structure_filters.GenericRoleFilter, DjangoFilterBackend)
    filterset_class = filters.ServiceFilter
    lookup_field = "uuid"
    get_yaml_method = "get_service_yaml"
    put_yaml_method = "put_service_yaml"
    delete_scope_method = "delete_service"
