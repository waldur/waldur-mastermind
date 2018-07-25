import datetime
from decimal import Decimal
import logging

from celery import shared_task
from dateutil.relativedelta import relativedelta
from django.contrib.contenttypes.models import ContentType
from django.core.mail import send_mail

from waldur_core.core import tasks as core_tasks, utils as core_utils
from waldur_core.monitoring.models import ResourceItem, ResourceSla, ResourceSlaStateTransition
from waldur_core.monitoring.utils import format_period

from .backend import ZabbixBackendError
from .models import Host, ITService, Item, SlaHistory

logger = logging.getLogger(__name__)


@shared_task(name='waldur_core.zabbix.pull_sla')
def pull_sla(host_uuid):
    """
    Pull SLAs for given Zabbix host for all time of its existence in Zabbix
    """
    try:
        host = Host.objects.get(uuid=host_uuid)
    except Host.DoesNotExist:
        logger.warning('Unable to pull SLA for host with UUID %s, because it is gone', host_uuid)
        return

    try:
        itservice = ITService.objects.get(host=host, is_main=True)
    except ITService.DoesNotExist:
        logger.warning('Unable to pull SLA for host with UUID %s, because IT service does not exist', host_uuid)
        return

    backend = itservice.get_backend()

    try:
        # Get dates of first and last service alarm
        min_dt, max_dt = backend.get_sla_range(itservice.backend_id)
    except ZabbixBackendError as e:
        logger.warning('Unable to pull SLA for host with with UUID %s because of database error: %s', host_uuid, e)
        return

    # Shift date to beginning of the month
    current_point = min_dt.replace(day=1)
    while current_point <= max_dt:
        period = format_period(current_point)
        start_time = core_utils.datetime_to_timestamp(current_point)
        current_point += relativedelta(months=+1)
        end_time = core_utils.datetime_to_timestamp(min(max_dt, current_point))
        update_itservice_sla.delay(itservice.pk, period, start_time, end_time)

    logger.debug('Successfully pulled SLA for host with with UUID %s', host_uuid)


@shared_task(name='waldur_core.zabbix.update_sla')
def update_sla(sla_type):
    if sla_type not in ('yearly', 'monthly'):
        logger.error('Requested unknown SLA type: %s' % sla_type)
        return

    dt = datetime.datetime.now()

    if sla_type == 'yearly':
        period = dt.year
        start_time = int(datetime.datetime.strptime('01/01/%s' % dt.year, '%d/%m/%Y').strftime("%s"))
    else:  # it's a monthly SLA update
        period = format_period(dt)
        month_start = datetime.datetime.strptime('01/%s/%s' % (dt.month, dt.year), '%d/%m/%Y')
        start_time = int(month_start.strftime("%s"))

    end_time = int(dt.strftime("%s"))

    for itservice in ITService.objects.all():
        update_itservice_sla.delay(itservice.pk, period, start_time, end_time)


@shared_task
def update_itservice_sla(itservice_pk, period, start_time, end_time):
    logger.debug('Updating SLAs for IT Service with PK %s. Period: %s, start_time: %s, end_time: %s',
                 itservice_pk, period, start_time, end_time)

    try:
        itservice = ITService.objects.get(pk=itservice_pk)
    except ITService.DoesNotExist:
        logger.warning('Unable to update SLA for IT Service with PK %s, because it is gone', itservice_pk)
        return

    backend = itservice.host.get_backend()

    try:
        current_sla = backend.get_sla(itservice.backend_id, start_time, end_time)
        entry, _ = SlaHistory.objects.get_or_create(itservice=itservice, period=period)
        entry.value = Decimal(current_sla)
        entry.save()

        # Save SLA if IT service is marked as main for host
        if itservice.host and itservice.host.scope and itservice.is_main:
            ResourceSla.objects.update_or_create(
                object_id=itservice.host.scope.id,
                content_type=ContentType.objects.get_for_model(itservice.host.scope),
                period=period,
                defaults={
                    'value': current_sla,
                    'agreed_value': itservice.agreed_sla
                }
            )

        if itservice.backend_trigger_id:
            # update connected events
            events = backend.get_trigger_events(itservice.backend_trigger_id, start_time, end_time)
            for event in events:
                event_state = 'U' if int(event['value']) == 0 else 'D'
                entry.events.get_or_create(
                    timestamp=int(event['timestamp']),
                    state=event_state
                )

                if itservice.host and itservice.host.scope and itservice.is_main:
                    ResourceSlaStateTransition.objects.get_or_create(
                        scope=itservice.host.scope,
                        period=period,
                        timestamp=int(event['timestamp']),
                        state=int(event['value']) == 0
                    )
    except ZabbixBackendError as e:
        logger.warning(
            'Unable to update SLA for IT Service %s (ID: %s). Reason: %s', itservice.name, itservice.backend_id, e)
    logger.debug('Successfully updated SLA for IT Service %s (ID: %s)', itservice.name, itservice.backend_id)


@shared_task(name='waldur_core.zabbix.update_monitoring_items')
def update_monitoring_items():
    """
    Regularly update value of monitored resources
    """
    hosts = Host.objects.filter(object_id__isnull=False, state=Host.States.OK)
    for host in hosts:
        for config in host.MONITORING_ITEMS_CONFIGS:
            update_host_scope_monitoring_items.delay(host.uuid.hex,
                                                     zabbix_item_key=config['zabbix_item_key'],
                                                     monitoring_item_name=config['monitoring_item_name'])
    logger.debug('Successfully scheduled monitoring data update for zabbix hosts.')


@shared_task
def update_host_scope_monitoring_items(host_uuid, zabbix_item_key, monitoring_item_name):
    host = Host.objects.get(uuid=host_uuid)
    if host.scope is None:
        return None
    value = None
    if Item.objects.filter(template__hosts=host, key=zabbix_item_key).exists():
        value = host.get_backend().get_item_last_value(host.backend_id, key=zabbix_item_key)
        ResourceItem.objects.update_or_create(
            object_id=host.scope.id,
            content_type=ContentType.objects.get_for_model(host.scope),
            name=monitoring_item_name,
            defaults={'value': value}
        )
        logger.debug('Successfully updated monitoring item %s for host %s (%s). Current value: %s.',
                     monitoring_item_name, host.visible_name, host.uuid.hex, value)
    else:
        logger.debug('Host %s (UUID: %s) does not have monitoring item %s.',
                     host.visible_name, host.uuid.hex, monitoring_item_name)
    return value


@shared_task(max_retries=60, default_retry_delay=60)
def after_creation_monitoring_item_update(host_uuid, config):
    item_value = update_host_scope_monitoring_items(
        host_uuid, config['zabbix_item_key'], config['monitoring_item_name'])
    return item_value in config.get('after_creation_update_terminate_values', []) or item_value is None


class SMSTask(core_tasks.Task):
    """ Send SMS to given mobile number based on service settings or django settings """

    def execute(self, settings, message, phone):
        sender = settings.get_option('sms_email_from')
        recipient = settings.get_option('sms_email_rcpt')

        if sender and recipient and '{phone}' in recipient:
            send_mail('', message, sender, [recipient.format(phone=phone)], fail_silently=True)
        else:
            logger.warning('SMS was not sent, because `sms_email_from` and `sms_email_rcpt` '
                           'were not configured properly.')


@shared_task(name='waldur_core.zabbix.pull_hosts')
def pull_hosts():
    pullable_hosts = Host.objects.exclude(backend_id='')  # Cannot pull hosts without backend_id
    for host in pullable_hosts.filter(state=Host.States.ERRED):
        serialized_host = core_utils.serialize_instance(host)
        core_tasks.BackendMethodTask().apply_async(
            args=(serialized_host, 'pull_host'),
            link=core_tasks.RecoverTask().si(serialized_host),
            link_error=core_tasks.ErrorMessageTask().s(serialized_host),
        )
    for host in Host.objects.filter(state=Host.States.OK):
        serialized_host = core_utils.serialize_instance(host)
        core_tasks.BackendMethodTask().apply_async(
            args=(serialized_host, 'pull_host'),
            link_error=core_tasks.ErrorStateTransitionTask().s(serialized_host)
        )


class UpdateSettingsCredentials(core_tasks.Task):

    def execute(self, service_settings, serialized_user):
        user = core_utils.deserialize_instance(serialized_user)
        service_settings.password = user.password
        service_settings.save()
