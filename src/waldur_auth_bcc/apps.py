from django.apps import AppConfig


class AuthBCCConfig(AppConfig):
    name = 'waldur_auth_bcc'
    verbose_name = 'BCC Authentication'

    def ready(self):
        pass
