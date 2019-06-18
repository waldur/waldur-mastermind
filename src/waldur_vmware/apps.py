from django.apps import AppConfig


class VMwareConfig(AppConfig):
    name = 'waldur_vmware'
    verbose_name = 'VMware'
    service_name = 'VMware'

    def ready(self):
        from waldur_core.structure import SupportedServices

        from .backend import VMwareBackend

        SupportedServices.register_backend(VMwareBackend)
