from django.apps import AppConfig
from django.contrib.auth import get_user_model


class SAML2Config(AppConfig):
    name = 'waldur_auth_saml2'
    verbose_name = 'Auth SAML2'

    def ready(self):
        from djangosaml2.signals import pre_user_save
        from . import handlers

        pre_user_save.connect(
            handlers.update_registration_method,
            sender=get_user_model(),
            dispatch_uid='waldur_auth_saml2.handlers.update_registration_method',
        )
