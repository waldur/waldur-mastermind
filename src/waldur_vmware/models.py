from __future__ import unicode_literals

from django.db import models
from django.utils.translation import ugettext_lazy as _

from waldur_core.structure import models as structure_models


class VMwareService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project,
        related_name='+',
        through='VMwareServiceProjectLink'
    )

    class Meta:
        unique_together = ('customer', 'settings')
        verbose_name = _('VMware provider')
        verbose_name_plural = _('VMware providers')

    @classmethod
    def get_url_name(cls):
        return 'vmware'


class VMwareServiceProjectLink(structure_models.ServiceProjectLink):

    service = models.ForeignKey(VMwareService)

    class Meta(structure_models.ServiceProjectLink.Meta):
        verbose_name = _('VMware provider project link')
        verbose_name_plural = _('VMware provider project links')

    @classmethod
    def get_url_name(cls):
        return 'vmware-spl'
