from django.apps import AppConfig
from django.db.models import signals


class MarketplaceAzureConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_azure'
    verbose_name = 'Marketplace Azure'

    def ready(self):
        from waldur_azure import models as azure_models
        from waldur_azure.apps import AzureConfig
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace import handlers as marketplace_handlers

        from . import handlers, processors, VIRTUAL_MACHINE_TYPE, SQL_SERVER_TYPE

        resource_models = (
            azure_models.VirtualMachine,
            azure_models.SQLServer,
            azure_models.SQLDatabase
        )

        marketplace_handlers.connect_resource_handlers(*resource_models)
        marketplace_handlers.connect_resource_metadata_handlers(*resource_models)

        signals.post_save.connect(
            handlers.synchronize_nic,
            sender=azure_models.NetworkInterface,
            dispatch_uid='waldur_mastermind.marketpace_azure.synchronize_nic',
        )

        signals.post_save.connect(
            handlers.synchronize_public_ip,
            sender=azure_models.PublicIP,
            dispatch_uid='waldur_mastermind.marketpace_azure.synchronize_public_ip',
        )

        manager.register(offering_type=VIRTUAL_MACHINE_TYPE,
                         create_resource_processor=processors.VirtualMachineCreateProcessor,
                         delete_resource_processor=processors.VirtualMachineDeleteProcessor,
                         service_type=AzureConfig.service_name)

        manager.register(offering_type=SQL_SERVER_TYPE,
                         create_resource_processor=processors.SQLServerCreateProcessor,
                         delete_resource_processor=processors.SQLServerDeleteProcessor,
                         service_type=AzureConfig.service_name)
