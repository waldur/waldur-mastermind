from django.apps import AppConfig


class PIDConfig(AppConfig):
    name = 'waldur_pid'
    verbose_name = 'PID'
    service_name = 'PID'

    def ready(self):
        pass
