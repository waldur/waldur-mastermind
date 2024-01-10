from django.apps import AppConfig


class AWSConfig(AppConfig):
    name = "waldur_aws"
    verbose_name = "Waldur AWS EC2"
    service_name = "Amazon"

    def ready(self):
        from waldur_core.structure.registry import SupportedServices

        from .backend import AWSBackend

        SupportedServices.register_backend(AWSBackend)
