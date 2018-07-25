import logging

from waldur_core.structure.models import NewResource as Resource

from . import executors
from .models import Host


logger = logging.getLogger(__name__)


def delete_hosts_on_scope_deletion(sender, instance, name, source, target, **kwargs):
    if target != Resource.States.DELETING:
        return
    for host in Host.objects.filter(scope=instance):
        if host.state == Host.States.OK:
            executors.HostDeleteExecutor.execute(host)
        elif host.state == Host.States.ERRED:
            executors.HostDeleteExecutor.execute(host, force=True)
        else:
            logger.exception(
                'Instance %s host was in state %s on instance deletion.', instance, host.human_readable_state)
            host.set_erred()
            host.save()
            executors.HostDeleteExecutor.execute(host, force=True)


def refresh_database_connection(sender, instance, created=False, **kwargs):
    if not created and instance.type == 'Zabbix' and instance.tracker.has_changed('options'):
        instance.get_backend()._get_db_connection(force=True)
