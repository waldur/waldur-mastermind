from django.apps import AppConfig


class DigitalOceanConfig(AppConfig):
    name = "waldur_digitalocean"
    verbose_name = "Waldur DigitalOcean"
    service_name = "DigitalOcean"

    def ready(self):
        from waldur_core.structure.registry import SupportedServices

        from .backend import DigitalOceanBackend

        SupportedServices.register_backend(DigitalOceanBackend)
