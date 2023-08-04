from django.apps import AppConfig


class PolicyConfig(AppConfig):
    name = 'waldur_mastermind.policy'
    verbose_name = 'Policy'

    def ready(self):
        from . import models

        models.Policy.set_all_handlers_for_subclasses()
