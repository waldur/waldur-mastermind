from django.apps import AppConfig


class MarketplaceVMwareConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_vmware'
    verbose_name = 'Marketplace VMware'

    def ready(self):
        from waldur_vmware import models as vmware_models
        from waldur_vmware.apps import VMwareConfig
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace import handlers as marketplace_handlers

        from . import processors, VIRTUAL_MACHINE_TYPE

        resource_models = (
            vmware_models.VirtualMachine,
        )

        marketplace_handlers.connect_resource_handlers(*resource_models)
        marketplace_handlers.connect_resource_metadata_handlers(*resource_models)

        manager.register(offering_type=VIRTUAL_MACHINE_TYPE,
                         create_resource_processor=processors.VirtualMachineCreateProcessor,
                         service_type=VMwareConfig.service_name)
