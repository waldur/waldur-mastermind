from django.apps import AppConfig


class AuthValimoConfig(AppConfig):
    name = 'waldur_auth_valimo'
    verbose_name = 'Waldur Auth Valimo'

    def ready(self):
        pass
