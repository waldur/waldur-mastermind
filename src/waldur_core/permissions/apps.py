from django.apps import AppConfig
from django.db.models import signals

from waldur_core.structure.signals import structure_role_granted, structure_role_revoked


class PermissionsConfig(AppConfig):
    name = 'waldur_core.permissions'
    verbose_name = 'Permissions'

    def ready(self):
        from waldur_core.structure.models import Customer, Project
        from waldur_mastermind.marketplace.models import OfferingPermission

        from . import handlers

        for model in (Customer, Project):
            structure_role_granted.connect(
                handlers.sync_permission_when_role_is_granted,
                sender=model,
                dispatch_uid='waldur_core.permissions.handlers.'
                'sync_permission_when_role_is_granted_%s' % model.__name__,
            )

            structure_role_revoked.connect(
                handlers.sync_permission_when_role_is_revoked,
                sender=model,
                dispatch_uid='waldur_core.permissions.handlers.'
                'sync_permission_when_role_is_revoked_%s' % model.__name__,
            )

        signals.post_save.connect(
            handlers.sync_offering_permission_creation,
            sender=OfferingPermission,
            dispatch_uid='waldur_core.permissions.handlers.'
            'sync_offering_permission_creation',
        )

        signals.post_delete.connect(
            handlers.sync_offering_permission_deletion,
            sender=OfferingPermission,
            dispatch_uid='waldur_core.permissions.handlers.'
            'sync_offering_permission_deletion',
        )
