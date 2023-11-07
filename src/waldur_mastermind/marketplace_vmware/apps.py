from django.apps import AppConfig


class MarketplaceVMwareConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_vmware'
    verbose_name = 'Marketplace VMware'

    def ready(self):
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_mastermind.marketplace.plugins import Component, manager
        from waldur_vmware import models as vmware_models
        from waldur_vmware import signals as vmware_signals
        from waldur_vmware.apps import VMwareConfig

        from . import VIRTUAL_MACHINE_TYPE, handlers, processors

        resource_models = (vmware_models.VirtualMachine,)

        marketplace_handlers.connect_resource_handlers(*resource_models)
        marketplace_handlers.connect_resource_metadata_handlers(*resource_models)

        LIMIT = marketplace_models.OfferingComponent.BillingTypes.LIMIT
        manager.register(
            offering_type=VIRTUAL_MACHINE_TYPE,
            create_resource_processor=processors.VirtualMachineCreateProcessor,
            service_type=VMwareConfig.service_name,
            can_update_limits=True,
            components=(
                Component(
                    type='cpu', name='CPU', measured_unit='vCPU', billing_type=LIMIT
                ),
                # Price is stored per GiB but size is stored per MiB
                # therefore we need to divide size by factor when price estimate is calculated.
                Component(
                    type='ram',
                    name='RAM',
                    measured_unit='GB',
                    billing_type=LIMIT,
                    factor=1024,
                ),
                Component(
                    type='disk',
                    name='Disk',
                    measured_unit='GB',
                    billing_type=LIMIT,
                    factor=1024,
                ),
            ),
        )

        vmware_signals.vm_updated.connect(
            handlers.update_marketplace_resource_limits_when_vm_is_updated,
            dispatch_uid='marketplace_vmware.handlers.'
            'update_marketplace_resource_limits_when_vm_is_updated',
        )
