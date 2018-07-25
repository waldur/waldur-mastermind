from django.apps import AppConfig
from django.db.models import signals


class AzureConfig(AppConfig):
    name = 'waldur_azure'
    verbose_name = 'Waldur Azure'
    service_name = 'Azure'
    is_public_service = True

    def ready(self):
        from waldur_core.structure import SupportedServices
        from backend import AzureBackend
        SupportedServices.register_backend(AzureBackend)

        from waldur_azure import models, handlers

        signals.post_save.connect(
            handlers.copy_cloud_service_name_on_service_creation,
            sender=models.AzureServiceProjectLink,
            dispatch_uid='waldur_azure.handlers.copy_cloud_service_name_on_service_creation',
        )
