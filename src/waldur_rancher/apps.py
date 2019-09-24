from django.apps import AppConfig


class RancherConfig(AppConfig):
    name = 'waldur_rancher'
    verbose_name = 'Rancher'
    service_name = 'Rancher'

    def ready(self):
        from waldur_core.structure import SupportedServices

        from .backend import RancherBackend

        SupportedServices.register_backend(RancherBackend)
