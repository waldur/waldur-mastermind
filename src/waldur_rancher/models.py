import logging
from urllib.parse import urljoin

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import ArrayField, JSONField
from django.db import models
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_core.structure.models import NewResource, ServiceSettings

logger = logging.getLogger(__name__)


class RancherService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project,
        related_name='rancher_services',
        through='RancherServiceProjectLink',
    )

    class Meta:
        unique_together = ('customer', 'settings')
        verbose_name = _('Rancher provider')
        verbose_name_plural = _('Rancher providers')

    @classmethod
    def get_url_name(cls):
        return 'rancher'


class RancherServiceProjectLink(structure_models.ServiceProjectLink):

    service = models.ForeignKey(on_delete=models.CASCADE, to=RancherService)

    class Meta(structure_models.ServiceProjectLink.Meta):
        verbose_name = _('Rancher provider project link')
        verbose_name_plural = _('Rancher provider project links')

    @classmethod
    def get_url_name(cls):
        return 'rancher-spl'


class BackendMixin(models.Model):
    """
    Mixin to add standard backend_id field.
    """

    class Meta:
        abstract = True

    backend_id = models.CharField(max_length=255, blank=True)


class SettingsMixin(models.Model):
    class Meta:
        abstract = True

    settings = models.ForeignKey(
        to='structure.ServiceSettings', on_delete=models.CASCADE, related_name='+',
    )

    def get_backend(self):
        return self.settings.get_backend()


class Cluster(SettingsMixin, NewResource):
    class RuntimeStates:
        ACTIVE = 'active'

    service_project_link = models.ForeignKey(
        RancherServiceProjectLink, related_name='k8s_clusters', on_delete=models.PROTECT
    )

    """
    Rancher generated node installation command base. For example:
    sudo docker run -d --privileged --restart=unless-stopped --net=host
    -v /etc/kubernetes:/etc/kubernetes -v /var/run:/var/run rancher/rancher-agent:v2.2.8
    --server https://192.168.33.13
    --token df8vrttmcmz8qzfbp74t6nkl5t5pbkrjh8wgkv27zrk8ldhfj6sp4w
    --ca-checksum e3596989da2fa5f8a7bdfbfd1079f87033217152db4dfc93532932b17aad1567
    --etcd --controlplane --worker
    """
    node_command = models.CharField(
        max_length=1024,
        blank=True,
        help_text='Rancher generated node installation command base.',
    )
    tracker = FieldTracker()
    tenant_settings = models.ForeignKey(
        to=structure_models.ServiceSettings,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    runtime_state = models.CharField(max_length=255, blank=True)

    @classmethod
    def get_url_name(cls):
        return 'rancher-cluster'

    def get_access_url(self):
        base_url = self.service_project_link.service.settings.backend_url
        return urljoin(base_url, 'c/' + self.backend_id)

    def __str__(self):
        return self.name


class RoleMixin(models.Model):
    controlplane_role = models.BooleanField(default=False)
    etcd_role = models.BooleanField(default=False)
    worker_role = models.BooleanField(default=False)

    class Meta:
        abstract = True


class Node(
    core_models.UuidMixin,
    core_models.NameMixin,
    structure_models.StructureModel,
    core_models.StateMixin,
    BackendMixin,
    RoleMixin,
    structure_models.StructureLoggableMixin,
    structure_models.TimeStampedModel,
):
    class RuntimeStates:
        ACTIVE = 'active'
        REGISTERING = 'registering'
        UNAVAILABLE = 'unavailable'

    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ContentType, null=True, related_name='+'
    )
    object_id = models.PositiveIntegerField(null=True)
    instance = GenericForeignKey(
        'content_type', 'object_id'
    )  # a virtual machine where will deploy k8s node.
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE)
    initial_data = JSONField(
        blank=True, default=dict, help_text=_('Initial data for instance creating.')
    )
    runtime_state = models.CharField(max_length=255, blank=True)
    k8s_version = models.CharField(max_length=255, blank=True)
    docker_version = models.CharField(max_length=255, blank=True)
    cpu_allocated = models.FloatField(blank=True, null=True)
    cpu_total = models.IntegerField(blank=True, null=True)
    ram_allocated = models.IntegerField(
        blank=True, null=True, help_text='Allocated RAM in Mi.'
    )
    ram_total = models.IntegerField(blank=True, null=True, help_text='Total RAM in Mi.')
    pods_allocated = models.IntegerField(blank=True, null=True)
    pods_total = models.IntegerField(blank=True, null=True)
    labels = JSONField(blank=True, default=dict)
    annotations = JSONField(blank=True, default=dict)

    tracker = FieldTracker()

    def get_node_command(self):
        roles_command = []
        if self.controlplane_role:
            roles_command.append('--controlplane')

        if self.etcd_role:
            roles_command.append('--etcd')

        if self.worker_role:
            roles_command.append('--worker')

        return self.cluster.node_command + ' ' + ' '.join(roles_command)

    class Meta:
        ordering = ('name',)
        unique_together = (('content_type', 'object_id'), ('cluster', 'name'))

    class Permissions:
        customer_path = 'cluster__service_project_link__project__customer'
        project_path = 'cluster__service_project_link__project'
        service_path = 'cluster__service_project_link__service'

    @classmethod
    def get_url_name(cls):
        return 'rancher-node'

    @property
    def service_project_link(self):
        return self.cluster.service_project_link

    def get_backend(self):
        return self.cluster.get_backend()

    def __str__(self):
        return self.name


class RancherUser(
    core_models.UuidMixin,
    BackendMixin,
    structure_models.StructureLoggableMixin,
    structure_models.StructureModel,
):
    user = models.ForeignKey(core_models.User, on_delete=models.PROTECT)
    clusters = models.ManyToManyField(Cluster, through='RancherUserClusterLink')
    settings = models.ForeignKey('structure.ServiceSettings', on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True)

    @staticmethod
    def make_random_password():
        return core_models.User.objects.make_random_password()

    class Meta:
        unique_together = (('user', 'settings'),)
        ordering = ('user__username',)

    class Permissions:
        customer_path = 'settings__customer'

    @classmethod
    def get_url_name(cls):
        return 'rancher-user'

    def __str__(self):
        return self.user.username


class ClusterRole(models.CharField):
    CLUSTER_OWNER = 'owner'
    CLUSTER_MEMBER = 'member'

    CHOICES = (
        (CLUSTER_OWNER, 'Cluster owner'),
        (CLUSTER_MEMBER, 'Cluster member'),
    )

    def __init__(self, *args, **kwargs):
        kwargs['max_length'] = 30
        kwargs['choices'] = self.CHOICES
        super(ClusterRole, self).__init__(*args, **kwargs)


class RancherUserClusterLink(BackendMixin):
    user = models.ForeignKey(RancherUser, on_delete=models.CASCADE)
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE)
    role = ClusterRole(db_index=True)

    class Meta:
        unique_together = (('user', 'cluster', 'role'),)


class RancherUserProjectLink(BackendMixin):
    user = models.ForeignKey(RancherUser, on_delete=models.CASCADE)
    project = models.ForeignKey('Project', on_delete=models.CASCADE)
    role = models.CharField(max_length=255, blank=False)

    class Meta:
        unique_together = (('user', 'project', 'role'),)


class Catalog(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    structure_models.TimeStampedModel,
    BackendMixin,
    SettingsMixin,
    core_models.RuntimeStateMixin,
):
    # Rancher supports global, cluster and project scope
    content_type = models.ForeignKey(
        on_delete=models.CASCADE, to=ContentType, null=True, related_name='+'
    )
    object_id = models.PositiveIntegerField(null=True)
    scope = GenericForeignKey('content_type', 'object_id')
    catalog_url = models.URLField()
    branch = models.CharField(max_length=255)
    commit = models.CharField(max_length=40, blank=True)
    username = models.CharField(max_length=255, blank=True)
    password = models.CharField(max_length=255, blank=True)

    def get_backend(self):
        return self.scope.get_backend()

    @property
    def scope_type(self):
        if isinstance(self.scope, ServiceSettings):
            return 'global'
        elif isinstance(self.scope, Cluster):
            return 'cluster'
        else:
            return 'project'

    def __str__(self):
        return self.name


class Project(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    structure_models.TimeStampedModel,
    BackendMixin,
    SettingsMixin,
    structure_models.StructureModel,
    core_models.RuntimeStateMixin,
):
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, related_name='+'
    )

    def __str__(self):
        return self.name

    class Permissions:
        customer_path = 'cluster__service_project_link__project__customer'
        project_path = 'cluster__service_project_link__project'
        service_path = 'cluster__service_project_link__service'

    @classmethod
    def get_url_name(cls):
        return 'rancher-project'


class Namespace(
    core_models.UuidMixin,
    core_models.NameMixin,
    structure_models.TimeStampedModel,
    BackendMixin,
    SettingsMixin,
    core_models.RuntimeStateMixin,
):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, related_name='namespaces'
    )

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return 'rancher-namespace'


# Rancher template used for application provisioning
class Template(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.UiDescribableMixin,
    structure_models.TimeStampedModel,
    BackendMixin,
    SettingsMixin,
    core_models.RuntimeStateMixin,
):
    catalog = models.ForeignKey(
        Catalog, on_delete=models.CASCADE, null=True, related_name='+'
    )
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, related_name='+'
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, related_name='+'
    )
    project_url = models.URLField(max_length=500, blank=True)
    default_version = models.CharField(max_length=255)
    versions = ArrayField(models.CharField(max_length=255))
    icon = models.FileField(upload_to='rancher_icons', blank=True, null=True)

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return 'rancher-template'

    class Meta:
        ordering = ('name',)


class Workload(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.RuntimeStateMixin,
    structure_models.TimeStampedModel,
    BackendMixin,
    SettingsMixin,
):
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, related_name='+'
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, related_name='+'
    )
    namespace = models.ForeignKey(
        Namespace, on_delete=models.CASCADE, null=True, related_name='+'
    )
    scale = models.PositiveSmallIntegerField()

    def __str__(self):
        return self.name

    class Permissions:
        customer_path = 'cluster__service_project_link__project__customer'
        project_path = 'cluster__service_project_link__project'
        service_path = 'cluster__service_project_link__service'

    class Meta:
        ordering = ('name',)

    @classmethod
    def get_url_name(cls):
        return 'rancher-workload'


class HPA(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    core_models.StateMixin,
    core_models.RuntimeStateMixin,
    structure_models.TimeStampedModel,
    BackendMixin,
    SettingsMixin,
):
    """
    HPA stands for Horizontal Pod Autoscaler.
    """

    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, null=True, related_name='+'
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, related_name='+'
    )
    namespace = models.ForeignKey(
        Namespace, on_delete=models.CASCADE, null=True, related_name='+'
    )
    workload = models.ForeignKey(
        Workload, on_delete=models.CASCADE, null=True, related_name='+'
    )
    current_replicas = models.PositiveSmallIntegerField(default=0)
    desired_replicas = models.PositiveSmallIntegerField(default=0)
    min_replicas = models.PositiveSmallIntegerField(default=0)
    max_replicas = models.PositiveSmallIntegerField(default=0)
    metrics = JSONField()

    def __str__(self):
        return self.name

    class Permissions:
        customer_path = 'cluster__service_project_link__project__customer'
        project_path = 'cluster__service_project_link__project'
        service_path = 'cluster__service_project_link__service'

    class Meta:
        ordering = ('name',)

    @classmethod
    def get_url_name(cls):
        return 'rancher-hpa'


# Waldur template is used for cluster provisioning, it doesn't have counterpart is Rancher
class ClusterTemplate(
    core_models.UuidMixin,
    core_models.NameMixin,
    core_models.DescribableMixin,
    structure_models.TimeStampedModel,
):
    class Meta:
        ordering = ('name',)

    @classmethod
    def get_url_name(cls):
        return 'rancher-cluster-template'


class ClusterTemplateNode(RoleMixin):
    template = models.ForeignKey(
        ClusterTemplate, on_delete=models.CASCADE, related_name='nodes'
    )
    min_vcpu = models.PositiveSmallIntegerField(verbose_name='Min vCPU (cores)')
    min_ram = models.PositiveIntegerField(verbose_name='Min RAM (GB)')
    system_volume_size = models.PositiveIntegerField(
        verbose_name='System volume size (GB)'
    )
    preferred_volume_type = models.CharField(max_length=150, blank=True)
