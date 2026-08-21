import logging
from typing import cast
from urllib.parse import urljoin

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMField, transition
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import models as core_models
from waldur_core.core.models import BackendMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure.models import BaseResource, ServiceSettings
from waldur_openstack import models as openstack_models
from waldur_rancher.enums import (
    ROLE_CHOICES,
    CatalogScopeType,
    KeycloakUserGroupMembershipState,
    RoleScopeType,
)

logger = logging.getLogger(__name__)


class SettingsMixin(models.Model):
    class Meta:
        abstract = True

    settings = models.ForeignKey[ServiceSettings](
        to="structure.ServiceSettings",
        on_delete=models.CASCADE,
        related_name="+",
    )

    def get_backend(self):
        return self.settings.get_backend()


class Cluster(SettingsMixin, BaseResource, core_models.AvailableMixin):
    class Meta(BaseResource.Meta):
        pass

    class RuntimeStates:
        ACTIVE = "active"

    id: int
    tracker = cast(FieldInstanceTracker, FieldTracker())
    tenant = models.ForeignKey(
        to=openstack_models.Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    runtime_state = models.CharField(max_length=255, blank=True)
    management_security_group = models.ForeignKey(
        to=openstack_models.SecurityGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # For Managed Rancher plugin OpenStack VMs are isolated from Rancher nodes
    vm_project = models.ForeignKey(
        to=structure_models.Project,
        on_delete=models.CASCADE,
    )

    node_set: models.Manager["Node"]

    kubernetes_version = models.CharField(
        max_length=50,
        blank=True,
        help_text=_("Kubernetes version used in the cluster."),
    )

    capacity = models.JSONField(
        blank=True,
        default=dict,
        help_text=_(
            "Cluster capacity in the format {'cpu': '10', 'ram': '49125240Ki', 'pods': '330'}"
        ),
    )

    requested = models.JSONField(
        blank=True,
        default=dict,
        help_text=_(
            "Cluster requested resources in the format {'cpu': '1450m', 'memory': '884Mi', 'pods': '13'}"
        ),
    )

    @property
    def linked_tenant_ids(self) -> list[int]:
        """
        Returns all tenants linked to this cluster.
        """
        return list(
            self.node_set.order_by()
            .values_list("instance__tenant_id", flat=True)
            .distinct()
        )

    @classmethod
    def get_url_name(cls):
        return "rancher-cluster"

    def get_access_url(self) -> str | None:
        base_url = self.service_settings.backend_url
        if base_url:
            return urljoin(base_url, "c/" + self.backend_id)

    def __str__(self):
        return self.name


class RoleMixin(models.Model):
    role = models.CharField(choices=ROLE_CHOICES, max_length=10, db_index=True)

    class Meta:
        abstract = True


class Node(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.StateMixin,
    BackendMixin,
    RoleMixin,
    structure_models.StructureLoggableMixin,
    TimeStampedModel,
):
    class RuntimeStates:
        ACTIVE = "active"
        REGISTERING = "registering"
        UNAVAILABLE = "unavailable"

    instance = models.ForeignKey(
        openstack_models.Instance,
        on_delete=models.CASCADE,
        null=True,
        related_name="+",
    )
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE)
    initial_data = models.JSONField(
        blank=True, default=dict, help_text=_("Initial data for instance creating.")
    )
    runtime_state = models.CharField(max_length=255, blank=True)
    k8s_version = models.CharField(max_length=255, blank=True)
    docker_version = models.CharField(max_length=255, blank=True)
    cpu_allocated = models.FloatField(blank=True, null=True)
    cpu_total = models.IntegerField(blank=True, null=True)
    ram_allocated = models.IntegerField(
        blank=True, null=True, help_text="Allocated RAM in Mi."
    )
    ram_total = models.IntegerField(blank=True, null=True, help_text="Total RAM in Mi.")
    pods_allocated = models.IntegerField(blank=True, null=True)
    pods_total = models.IntegerField(blank=True, null=True)
    labels = models.JSONField(blank=True, default=dict)
    annotations = models.JSONField(blank=True, default=dict)

    tracker = cast(FieldInstanceTracker, FieldTracker())

    cluster_id: int

    class Meta:
        ordering = ["name", "id"]
        unique_together = ("cluster", "name")

    class Permissions:
        customer_path = "cluster__project__customer"
        project_path = "cluster__project"

    @classmethod
    def get_url_name(cls):
        return "rancher-node"

    def get_backend(self):
        return self.cluster.get_backend()

    def __str__(self):
        return self.name


class RancherUser(
    core_models.UuidMixin,
    BackendMixin,
    structure_models.StructureLoggableMixin,
):
    user = models.ForeignKey(core_models.User, on_delete=models.CASCADE)
    clusters = models.ManyToManyField(Cluster, through="RancherUserClusterLink")
    settings = models.ForeignKey[ServiceSettings](
        "structure.ServiceSettings", on_delete=models.PROTECT
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = (("user", "settings"),)
        ordering = ["user__username", "id"]

    class Permissions:
        customer_path = "settings__customer"

    @classmethod
    def get_url_name(cls):
        return "rancher-user"

    def __str__(self):
        return self.user.username


class RoleTemplate(SettingsMixin, core_models.UuidMixin):
    scope_type = models.CharField(
        choices=RoleScopeType.CHOICES, max_length=10, db_index=True
    )
    name = models.CharField(max_length=50, help_text=_("Role internal name"))
    display_name = models.CharField(max_length=50, help_text=_("Role public name"))

    def __str__(self):
        return self.name

    class Meta:
        unique_together = (("scope_type", "name", "settings"),)


class RancherUserClusterLink(BackendMixin):
    user = models.ForeignKey(RancherUser, on_delete=models.CASCADE)
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE)
    role = models.ForeignKey(RoleTemplate, on_delete=models.CASCADE)

    class Meta:
        unique_together = (("user", "cluster", "role"),)


class RancherUserProjectLink(BackendMixin):
    user = models.ForeignKey(RancherUser, on_delete=models.CASCADE)
    project = models.ForeignKey["Project"]("Project", on_delete=models.CASCADE)
    role = models.ForeignKey(RoleTemplate, on_delete=models.CASCADE)

    class Meta:
        unique_together = (("user", "project", "role"),)


class Catalog(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
    BackendMixin,
    SettingsMixin,
    core_models.RuntimeStateMixin,
):
    # Rancher supports global, cluster and project scope
    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ContentType, null=True, related_name="+"
    )
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey("content_type", "object_id")
    catalog_url = models.URLField()
    branch = models.CharField(max_length=255)
    commit = models.CharField(max_length=40, blank=True)
    username = models.CharField(max_length=255, blank=True)
    password = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created", "id"]

    def get_backend(self):
        scope = cast(ServiceSettings | Cluster, self.scope)
        return scope.get_backend()

    @property
    def scope_type(self) -> CatalogScopeType:
        if isinstance(self.scope, ServiceSettings):
            return "global"
        elif isinstance(self.scope, Cluster):
            return "cluster"
        else:
            return "project"

    def __str__(self):
        return self.name


class Project(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
    BackendMixin,
    SettingsMixin,
    core_models.RuntimeStateMixin,
):
    namespaces: models.Manager["Namespace"]

    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, related_name="+"
    )

    def __str__(self):
        return self.name

    class Permissions:
        customer_path = "cluster__project__customer"
        project_path = "cluster__project"

    @classmethod
    def get_url_name(cls):
        return "rancher-project"


class Namespace(
    core_models.UuidMixin,
    core_models.NameMixin,
    TimeStampedModel,
    BackendMixin,
    SettingsMixin,
    core_models.RuntimeStateMixin,
):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, related_name="namespaces"
    )

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "rancher-namespace"


# Rancher template used for application provisioning
class Template(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.UiDescribableMixin,
    TimeStampedModel,
    BackendMixin,
    SettingsMixin,
    core_models.RuntimeStateMixin,
):
    catalog = models.ForeignKey(
        Catalog, on_delete=models.CASCADE, null=True, related_name="+"
    )
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, related_name="+"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, related_name="+"
    )
    project_url = models.URLField(max_length=500, blank=True)
    default_version = models.CharField(max_length=255)
    versions = ArrayField(models.CharField(max_length=255))
    icon = models.FileField(upload_to="rancher_icons", blank=True, null=True)

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "rancher-template"

    class Meta:
        ordering = ["name", "id"]


class Workload(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.RuntimeStateMixin,
    TimeStampedModel,
    BackendMixin,
    SettingsMixin,
):
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, related_name="+"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, related_name="+"
    )
    namespace = models.ForeignKey(
        Namespace, on_delete=models.CASCADE, null=True, related_name="+"
    )
    scale = models.PositiveSmallIntegerField()

    def __str__(self):
        return self.name

    class Permissions:
        customer_path = "cluster__project__customer"
        project_path = "cluster__project"

    class Meta:
        ordering = ["name", "id"]

    @classmethod
    def get_url_name(cls):
        return "rancher-workload"


class HPA(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    core_models.StateMixin,
    core_models.RuntimeStateMixin,
    TimeStampedModel,
    BackendMixin,
    SettingsMixin,
):
    """
    HPA stands for Horizontal Pod Autoscaler.
    """

    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, related_name="+"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, related_name="+"
    )
    namespace = models.ForeignKey(
        Namespace, on_delete=models.CASCADE, null=True, related_name="+"
    )
    workload = models.ForeignKey(
        Workload, on_delete=models.CASCADE, null=True, related_name="+"
    )
    current_replicas = models.PositiveSmallIntegerField(default=0)
    desired_replicas = models.PositiveSmallIntegerField(default=0)
    min_replicas = models.PositiveSmallIntegerField(default=0)
    max_replicas = models.PositiveSmallIntegerField(default=0)
    metrics = models.JSONField()

    def __str__(self):
        return self.name

    class Permissions:
        customer_path = "cluster__project__customer"
        project_path = "cluster__project"

    class Meta:
        ordering = ["name", "id"]

    @classmethod
    def get_url_name(cls):
        return "rancher-hpa"


# Waldur template is used for cluster provisioning, it doesn't have counterpart is Rancher
class ClusterTemplate(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    TimeStampedModel,
):
    nodes: models.Manager["ClusterTemplateNode"]

    class Meta:
        ordering = ["name", "id"]

    @classmethod
    def get_url_name(cls):
        return "rancher-cluster-template"


class ClusterTemplateNode(RoleMixin):
    template = models.ForeignKey(
        ClusterTemplate, on_delete=models.CASCADE, related_name="nodes"
    )
    min_vcpu = models.PositiveSmallIntegerField(verbose_name="Min vCPU (cores)")
    min_ram = models.PositiveIntegerField(verbose_name="Min RAM (GB)")
    system_volume_size = models.PositiveIntegerField(
        verbose_name="System volume size (GB)"
    )
    preferred_volume_type = models.CharField(max_length=150, blank=True)


class Application(SettingsMixin, core_models.RuntimeStateMixin, BaseResource):
    class Meta(BaseResource.Meta):
        pass

    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE)
    template = models.ForeignKey(Template, on_delete=models.CASCADE)
    rancher_project = models.ForeignKey(Project, on_delete=models.CASCADE)
    namespace = models.ForeignKey(Namespace, on_delete=models.CASCADE)
    version = models.CharField(max_length=100)
    answers = models.JSONField(blank=True, default=dict)

    @classmethod
    def get_url_name(cls):
        return "rancher-app"

    def __str__(self):
        return self.name

    @property
    def external_url(self) -> str | None:
        base_url = self.settings.backend_url
        if base_url:
            return f"{base_url.strip('/')}/p/{self.project.backend_id}/apps/{self.backend_id}"


class Ingress(SettingsMixin, core_models.RuntimeStateMixin, BaseResource):
    class Meta(BaseResource.Meta):
        pass

    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE)
    namespace = models.ForeignKey(Namespace, on_delete=models.CASCADE)
    rancher_project = models.ForeignKey(Project, on_delete=models.CASCADE)
    rules = models.JSONField(blank=True, default=list)

    @classmethod
    def get_url_name(cls):
        return "rancher-ingress"

    def __str__(self):
        return self.name


class Service(SettingsMixin, core_models.RuntimeStateMixin, BaseResource):
    class Meta(BaseResource.Meta):
        pass

    namespace = models.ForeignKey(Namespace, on_delete=models.CASCADE)
    cluster_ip = models.GenericIPAddressField(protocol="IPv4", blank=True, null=True)
    target_workloads = models.ManyToManyField(Workload)
    selector = models.JSONField(blank=True, null=True)

    @classmethod
    def get_url_name(cls):
        return "rancher-service"

    def __str__(self):
        return self.name


class KeycloakGroup(
    core_models.UuidMixin,
    core_models.BackendMixin,
    TimeStampedModel,
):
    name = models.CharField(_("Group name"), max_length=150, blank=True)
    scope_uuid = models.UUIDField(help_text=_("UUID of the cluster or project"))
    role = models.ForeignKey(RoleTemplate, on_delete=models.CASCADE)

    class Meta:
        unique_together = (("scope_uuid", "role"),)

    def __str__(self):
        return self.name


class KeycloakUserGroupMembership(
    core_models.UuidMixin, TimeStampedModel, core_models.ErrorMessageMixin
):
    username = models.CharField(max_length=255, help_text=_("Keycloak user username"))
    email = models.EmailField(help_text=_("User's email for notifications"))
    state = FSMField(
        choices=KeycloakUserGroupMembershipState.CHOICES,
        default=KeycloakUserGroupMembershipState.PENDING,
    )
    last_checked = models.DateTimeField(auto_now=True)
    group = models.ForeignKey(to=KeycloakGroup, on_delete=models.CASCADE)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)

    @transition(
        field=state,
        source=KeycloakUserGroupMembershipState.PENDING,
        target=KeycloakUserGroupMembershipState.ACTIVE,
    )
    def activate(self):
        pass

    def refresh_last_checked(self):
        self.last_checked = timezone.now()

    def __str__(self):
        return f"{self.username} in {self.group}"


class ClusterSecurityGroup(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
):
    class Permissions:
        customer_path = "cluster__project__customer"
        project_path = "cluster__project"

    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name="+")


class ClusterSecurityGroupRule(
    core_models.UuidMixin, openstack_models.BaseSecurityGroupRule
):
    group = models.ForeignKey(
        ClusterSecurityGroup, on_delete=models.CASCADE, related_name="rules"
    )


class ClusterPublicIP(
    core_models.UuidMixin,
    TimeStampedModel,
):
    cluster = models.ForeignKey(
        to=Cluster, on_delete=models.CASCADE, related_name="public_ips"
    )
    floating_ip = models.OneToOneField(
        to=openstack_models.FloatingIP,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    class Meta:
        unique_together = (("cluster", "floating_ip"),)
