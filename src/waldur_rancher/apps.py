from django.apps import AppConfig
from django.db.models import signals


class RancherConfig(AppConfig):
    name = 'waldur_rancher'
    verbose_name = 'Rancher'
    service_name = 'Rancher'

    def ready(self):
        from waldur_core.structure import SupportedServices

        from .backend import RancherBackend
        from . import handlers, models, signals as rancher_signals

        SupportedServices.register_backend(RancherBackend)

        rancher_signals.rancher_user_created.connect(
            handlers.notify_create_user,
            sender=models.RancherUser,
            dispatch_uid='waldur_rancher.notify_create_user',
        )

        signals.post_delete.connect(
            handlers.delete_catalog_when_cluster_is_deleted,
            sender=models.Cluster,
            dispatch_uid='waldur_rancher.delete_catalog_when_cluster_is_deleted',
        )
