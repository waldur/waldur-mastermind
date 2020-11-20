from django.apps import AppConfig


class GoogleConfig(AppConfig):
    name = 'waldur_mastermind.google'
    verbose_name = 'Google API'

    def ready(self):
        pass
