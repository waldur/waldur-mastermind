from django.apps import AppConfig


class RemoteWaldurConfig(AppConfig):
    name = 'waldur_waldur'
    verbose_name = 'Waldur on Waldur'
    service_name = 'WaldurRemote'
    is_public_service = True

    def ready(self):
        from waldur_core.structure import SupportedServices
        from .backend import WaldurBackend

        SupportedServices.register_backend(WaldurBackend)
