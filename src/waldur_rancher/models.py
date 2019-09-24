from __future__ import unicode_literals

import logging

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from model_utils.models import TimeStampedModel

from waldur_core.core.models import UuidMixin
from waldur_core.structure import models as structure_models
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

    service = models.ForeignKey(RancherService)

    class Meta(structure_models.ServiceProjectLink.Meta):
        verbose_name = _('Rancher provider project link')
        verbose_name_plural = _('Rancher provider project links')

    @classmethod
    def get_url_name(cls):
        return 'rancher-spl'


@python_2_unicode_compatible
class Cluster(NewResource):
    backend_id = models.CharField(max_length=255, blank=True, null=True)
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

    class Meta(object):
        unique_together = (('service_project_link', 'backend_id'), ('service_project_link', 'name'))

    @classmethod
    def get_url_name(cls):
        return 'rancher-cluster'

    def __str__(self):
        return self.name


class Node(TimeStampedModel, UuidMixin):
    content_type = models.ForeignKey(ContentType, null=True, related_name='+')
    object_id = models.PositiveIntegerField(null=True)
    instance = GenericForeignKey('content_type', 'object_id')  # a virtual machine where will deploy k8s node.
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE)

    class Meta(object):
        unique_together = ('content_type', 'object_id')

    class Permissions(object):
        customer_path = 'cluster__service_project_link__project__customer'
        project_path = 'cluster__service_project_link__project'
        service_path = 'cluster__service_project_link__service'

    @classmethod
    def get_url_name(cls):
        return 'rancher-node'
