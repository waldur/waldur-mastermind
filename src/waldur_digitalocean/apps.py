from django.apps import AppConfig
from django.db.models import signals


class DigitalOceanConfig(AppConfig):
    name = 'waldur_digitalocean'
    verbose_name = 'Waldur DigitalOcean'
    service_name = 'DigitalOcean'
    is_public_service = True

    def ready(self):
        from waldur_core.core import models as core_models
        from waldur_core.structure import SupportedServices, signals as structure_signals, models as structure_models

        from . import handlers
        from .backend import DigitalOceanBackend
        SupportedServices.register_backend(DigitalOceanBackend)

        for model in (structure_models.Project, structure_models.Customer):
            structure_signals.structure_role_revoked.connect(
                handlers.remove_ssh_keys_from_service,
                sender=model,
                dispatch_uid=('waldur_digitalocean.handlers.remove_ssh_keys_from_service__%s'
                              % model.__name__),
            )

        signals.pre_delete.connect(
            handlers.remove_ssh_key_from_service_settings_on_deletion,
            sender=core_models.SshPublicKey,
            dispatch_uid='waldur_digitalocean.handlers.remove_ssh_key_from_service_settings_on_deletion',
        )
