from django.apps import AppConfig
from django.db.models import signals


class MarketplaceVMwareConfig(AppConfig):
    name = 'waldur_mastermind.marketplace_vmware'
    verbose_name = 'Marketplace VMware'

    def ready(self):
        from waldur_mastermind.marketplace.plugins import Component
        from waldur_mastermind.marketplace.plugins import manager
        from waldur_mastermind.marketplace import handlers as marketplace_handlers
        from waldur_mastermind.invoices import registrators
        from waldur_mastermind.marketplace import models as marketplace_models
        from waldur_vmware import models as vmware_models
        from waldur_vmware import signals as vmware_signals
        from waldur_vmware.apps import VMwareConfig

        from . import handlers, registrators as vmware_registrators, processors, VIRTUAL_MACHINE_TYPE

        resource_models = (
            vmware_models.VirtualMachine,
        )

        marketplace_handlers.connect_resource_handlers(*resource_models)
        marketplace_handlers.connect_resource_metadata_handlers(*resource_models)

        USAGE = marketplace_models.OfferingComponent.BillingTypes.USAGE
        manager.register(offering_type=VIRTUAL_MACHINE_TYPE,
                         create_resource_processor=processors.VirtualMachineCreateProcessor,
                         service_type=VMwareConfig.service_name,
                         components=(
                             Component(type='cpu', name='CPU', measured_unit='vCPU', billing_type=USAGE),
                             # Price is stored per GiB but size is stored per MiB
                             # therefore we need to divide size by factor when price estimate is calculated.
                             Component(type='ram', name='RAM', measured_unit='GB', billing_type=USAGE, factor=1024),
                             Component(type='disk', name='Disk', measured_unit='GB', billing_type=USAGE, factor=1024),
                         ))

        registrators.RegistrationManager.add_registrator(
            vmware_models.VirtualMachine,
            vmware_registrators.VirtualMachineRegistrator
        )

        vmware_signals.vm_created.connect(
            handlers.add_new_vm_to_invoice,
            dispatch_uid='marketplace_vmware.handlers.add_new_vm_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.terminate_invoice_when_vm_deleted,
            sender=vmware_models.VirtualMachine,
            dispatch_uid='marketplace_vmware.handlers.terminate_invoice_when_vm_deleted',
        )

        vmware_signals.vm_updated.connect(
            handlers.create_invoice_item_when_vm_is_updated,
            dispatch_uid='marketplace_vmware.handlers.create_invoice_item_when_vm_is_updated',
        )

        vmware_signals.vm_updated.connect(
            handlers.update_marketplace_resource_limits_when_vm_is_updated,
            dispatch_uid='marketplace_vmware.handlers.'
                         'update_marketplace_resource_limits_when_vm_is_updated',
        )
