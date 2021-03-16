from django.apps import AppConfig


class AzureConfig(AppConfig):
    name = 'waldur_azure'
    verbose_name = 'Waldur Azure'
    service_name = 'Azure'

    def ready(self):
        from waldur_core.structure import SupportedServices
        from .backend import AzureBackend

        SupportedServices.register_backend(AzureBackend)
