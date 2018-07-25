from django.apps import AppConfig


class RijkscloudConfig(AppConfig):
    name = 'waldur_rijkscloud'
    verbose_name = 'Rijkscloud'
    service_name = 'Rijkscloud'

    def ready(self):
        from waldur_core.structure import SupportedServices

        from .backend import RijkscloudBackend

        SupportedServices.register_backend(RijkscloudBackend)
