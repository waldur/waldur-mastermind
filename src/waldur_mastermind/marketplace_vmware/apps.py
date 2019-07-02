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
                             Component(type='cpu_usage', name='CPU', measured_unit='hours', billing_type=USAGE),
                             Component(type='gpu_usage', name='GPU', measured_unit='hours', billing_type=USAGE),
                             Component(type='ram_usage', name='RAM', measured_unit='GB', billing_type=USAGE),
                         ))

        registrators.RegistrationManager.add_registrator(
            vmware_models.VirtualMachine,
            vmware_registrators.VirtualMachineRegistrator
        )

        signals.post_save.connect(
            handlers.add_new_vm_to_invoice,
            sender=marketplace_models.OrderItem,
            dispatch_uid='marketplace_vmware.handlers.add_new_vm_to_invoice',
        )

        signals.pre_delete.connect(
            handlers.terminate_invoice_when_vm_deleted,
            sender=vmware_models.VirtualMachine,
            dispatch_uid='marketplace_vmware.handlers.terminate_invoice_when_vm_deleted',
        )
