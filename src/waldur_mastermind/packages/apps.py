from django.apps import AppConfig


class PackageConfig(AppConfig):
    name = 'waldur_mastermind.packages'
    verbose_name = 'VPC packages'

    def ready(self):
        pass
