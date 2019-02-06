from django.apps import AppConfig


class MarketplaceAzureConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_azure'
    verbose_name = 'Marketplace Azure'

    def ready(self):
        from waldur_core.structure import models as structure_models
        from waldur_azure import models as azure_models
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace import handlers as marketplace_handlers

        from . import processors, VIRTUAL_MACHINE_TYPE, SQL_SERVER_TYPE

        marketplace_handlers.connect_resource_handlers(
            azure_models.VirtualMachine,
            azure_models.SQLServer,
            azure_models.SQLDatabase
        )

        manager.register(offering_type=VIRTUAL_MACHINE_TYPE,
                         create_resource_processor=processors.VirtualMachineCreateProcessor,
                         delete_resource_processor=processors.VirtualMachineDeleteProcessor,
                         scope_model=structure_models.ServiceSettings)

        manager.register(offering_type=SQL_SERVER_TYPE,
                         create_resource_processor=processors.SQLServerCreateProcessor,
                         delete_resource_processor=processors.SQLServerDeleteProcessor,
                         scope_model=structure_models.ServiceSettings)
