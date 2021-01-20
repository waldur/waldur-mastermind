from django.db import models
from django.utils.translation import ugettext_lazy as _

from waldur_core.structure import models as structure_models


class RemoteWaldurService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project,
        related_name='remote_waldur_services',
        through='RemoteWaldurServiceProjectLink',
    )

    class Meta:
        unique_together = ('customer', 'settings')
        verbose_name = _('Remote Waldur provider')
        verbose_name_plural = _('Remote Waldur providers')

    @classmethod
    def get_url_name(cls):
        return 'remote-waldur'


class RemoteWaldurServiceProjectLink(structure_models.ServiceProjectLink):
    service = models.ForeignKey(on_delete=models.CASCADE, to=RemoteWaldurService)

    class Meta(structure_models.ServiceProjectLink.Meta):
        verbose_name = _('Remote Waldur provider project link')
        verbose_name_plural = _('Remote Waldur provider project links')

    @classmethod
    def get_url_name(cls):
        return 'remote-waldur-spl'
