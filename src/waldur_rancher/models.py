import logging

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.fields import JSONField
from django.db import models
from django.utils.translation import ugettext_lazy as _
from model_utils import FieldTracker

from waldur_core.structure import models as structure_models
from waldur_core.core import models as core_models
from waldur_core.structure.models import NewResource

logger = logging.getLogger(__name__)


class RancherService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project, related_name='rancher_services', through='RancherServiceProjectLink')

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


class Cluster(NewResource):
    service_project_link = models.ForeignKey(
        RancherServiceProjectLink, related_name='k8s_clusters', on_delete=models.PROTECT)

    """
    Rancher generated node installation command base. For example:
    sudo docker run -d --privileged --restart=unless-stopped --net=host
    -v /etc/kubernetes:/etc/kubernetes -v /var/run:/var/run rancher/rancher-agent:v2.2.8
    --server https://192.168.33.13
    --token df8vrttmcmz8qzfbp74t6nkl5t5pbkrjh8wgkv27zrk8ldhfj6sp4w
    --ca-checksum e3596989da2fa5f8a7bdfbfd1079f87033217152db4dfc93532932b17aad1567
    --etcd --controlplane --worker
    """
    node_command = models.CharField(max_length=1024, blank=True,
                                    help_text='Rancher generated node installation command base.')
    tracker = FieldTracker()
    tenant_settings = models.ForeignKey(
        to=structure_models.ServiceSettings,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    runtime_state = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = (('service_project_link', 'backend_id'), ('service_project_link', 'name'))

    @classmethod
    def get_url_name(cls):
        return 'rancher-cluster'

    def get_access_url(self):
        return self.service_project_link.service.settings.backend_url

    def __str__(self):
        return self.name


class Node(core_models.UuidMixin,
           core_models.NameMixin,
           structure_models.StructureModel,
           core_models.StateMixin,
           structure_models.TimeStampedModel):
    content_type = models.ForeignKey(on_delete=models.CASCADE, to=ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    instance = GenericForeignKey('content_type', 'object_id')  # a virtual machine where will deploy k8s node.
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE)
    controlplane_role = models.BooleanField(default=False)
    etcd_role = models.BooleanField(default=False)
    worker_role = models.BooleanField(default=False)
    backend_id = models.CharField(max_length=255, blank=True)
    initial_data = JSONField(blank=True,
                             default=dict,
                             help_text=_('Initial data for instance creating.'))
    runtime_state = models.CharField(max_length=255, blank=True)
    k8s_version = models.CharField(max_length=255, blank=True)
    docker_version = models.CharField(max_length=255, blank=True)
    cpu_allocated = models.FloatField(blank=True, null=True)
    cpu_total = models.IntegerField(blank=True, null=True)
    ram_allocated = models.IntegerField(blank=True, null=True, help_text='Allocated RAM in Mi.')
    ram_total = models.IntegerField(blank=True, null=True, help_text='Total RAM in Mi.')
    pods_allocated = models.IntegerField(blank=True, null=True)
    pods_total = models.IntegerField(blank=True, null=True)
    labels = JSONField(blank=True, default=dict)
    annotations = JSONField(blank=True, default=dict)

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
