from __future__ import unicode_literals

from django.apps import AppConfig
from django.db.models import signals
from django_fsm import signals as fsm_signals


class ZabbixConfig(AppConfig):
    name = 'waldur_zabbix'
    verbose_name = 'Waldur Zabbix'
    service_name = 'Zabbix'

    def ready(self):
        from waldur_core.structure import SupportedServices, models as structure_models
        # structure
        from .backend import ZabbixBackend
        SupportedServices.register_backend(ZabbixBackend)

        from . import handlers
        for index, resource_model in enumerate(structure_models.ResourceMixin.get_all_models()):

            fsm_signals.post_transition.connect(
                handlers.delete_hosts_on_scope_deletion,
                sender=resource_model,
                dispatch_uid='waldur_zabbix.handlers.delete_hosts_on_scope_deletion_%s_%s' % (
                    index, resource_model.__name__)
            )

        signals.post_save.connect(
            handlers.refresh_database_connection,
            sender=structure_models.ServiceSettings,
            dispatch_uid='waldur_zabbix.handlers.refresh_database_connection',
        )
