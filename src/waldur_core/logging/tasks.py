import datetime
import logging
import tarfile
import traceback

from celery import shared_task
from django.conf import settings
from django.utils import timezone
import six

from waldur_core.core.utils import deserialize_instance
from waldur_core.logging.loggers import alert_logger, event_logger
from waldur_core.logging.models import BaseHook, Alert, AlertThresholdMixin, SystemNotification, Report
from waldur_core.logging.utils import create_report_archive
from waldur_core.structure import models as structure_models

logger = logging.getLogger(__name__)


@shared_task(name='waldur_core.logging.process_event')
def process_event(event):
    for hook in BaseHook.get_active_hooks():
        if check_event(event, hook):
            hook.process(event)

    try:
        project_uuid = event['context'].get('project_uuid')
        project = project_uuid and structure_models.Project.objects.get(uuid=project_uuid)
        customer_uuid = event['context'].get('customer_uuid')
        customer = customer_uuid and structure_models.Customer.objects.get(uuid=customer_uuid)
    except structure_models.Project.DoesNotExist:
        return
    except structure_models.Customer.DoesNotExist:
        return

    for hook in SystemNotification.get_hooks(event['type'], project=project, customer=customer):
        if check_event(event, hook):
            hook.process(event)


def check_event(event, hook):
    # Check that event matches with hook
    if event['type'] not in hook.all_event_types:
        return False
    for key, uuids in event_logger.get_permitted_objects_uuids(hook.user).items():
        if key in event['context'] and event['context'][key] in uuids:
            return True
    return False


@shared_task(name='waldur_core.logging.close_alerts_without_scope')
def close_alerts_without_scope():
    for alert in Alert.objects.filter(closed__isnull=True).iterator():
        if alert.scope is None:
            logger.error('Alert without scope was not closed. Alert id: %s.', alert.id)
            alert.close()


@shared_task(name='waldur_core.logging.alerts_cleanup')
def alerts_cleanup():
    timespan = settings.WALDUR_CORE.get('CLOSED_ALERTS_LIFETIME')
    if timespan:
        Alert.objects.filter(closed__lte=timezone.now() - timespan).delete()


@shared_task(name='waldur_core.logging.check_threshold')
def check_threshold():
    for model in AlertThresholdMixin.get_all_models():
        for obj in model.get_checkable_objects().filter(threshold__gt=0).iterator():
            if obj.is_over_threshold() and obj.scope:
                alert_logger.threshold.warning(
                    'Threshold for {scope_name} is exceeded.',
                    scope=obj.scope,
                    alert_type='threshold_exceeded',
                    alert_context={
                        'object': obj
                    })
            else:
                alert_logger.threshold.close(
                    scope=obj.scope,
                    alert_type='threshold_exceeded')


@shared_task(name='waldur_core.logging.create_report')
def create_report(serialized_report):
    report = deserialize_instance(serialized_report)

    today = datetime.datetime.today()
    timestamp = today.strftime('%Y%m%dT%H%M%S')
    archive_filename = 'waldur-logs-%s-%s.tar.gz' % (timestamp, report.uuid.hex)

    try:
        cf = create_report_archive(
            settings.WALDUR_CORE['LOGGING_REPORT_DIRECTORY'],
            settings.WALDUR_CORE['LOGGING_REPORT_INTERVAL'],
        )
    except (tarfile.TarError, OSError, ValueError) as e:
        report.state = Report.States.ERRED
        error_message = 'Error message: %s. Traceback: %s' % (six.text_type(e), traceback.format_exc())
        report.error_message = error_message
        report.save()
    else:
        report.file.save(archive_filename, cf)
        report.file_size = cf.size
        report.state = Report.States.DONE
        report.save()
