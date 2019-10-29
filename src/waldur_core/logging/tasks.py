import datetime
import logging
import tarfile
import traceback

from celery import shared_task
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from waldur_core.core.utils import deserialize_instance
from waldur_core.logging.models import BaseHook, SystemNotification, Report, Feed, Event
from waldur_core.logging.utils import create_report_archive
from waldur_core.structure import models as structure_models

logger = logging.getLogger(__name__)


@shared_task(name='waldur_core.logging.process_event')
def process_event(event_id):
    event = Event.objects.get(id=event_id)
    for hook in BaseHook.get_active_hooks():
        if check_event(event, hook):
            hook.process(event)

    process_system_notification(event)


def process_system_notification(event):
    project_ct = ContentType.objects.get_for_model(structure_models.Project)
    project_feed = Feed.objects.filter(event=event, content_type=project_ct).first()
    project = project_feed and project_feed.scope

    customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
    customer_feed = Feed.objects.filter(event=event, content_type=customer_ct).first()
    customer = customer_feed and customer_feed.scope

    for hook in SystemNotification.get_hooks(event.event_type, project=project, customer=customer):
        if check_event(event, hook):
            hook.process(event)


def check_event(event, hook):
    # Check that event matches with hook
    if event.event_type not in hook.all_event_types:
        return False

    # Check permissions
    for feed in Feed.objects.filter(event=event):
        qs = feed.content_type.model_class().get_permitted_objects(hook.user)
        if qs.filter(id=feed.object_id).exists():
            return True

    return False


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
        error_message = 'Error message: %s. Traceback: %s' % (str(e), traceback.format_exc())
        report.error_message = error_message
        report.save()
    else:
        report.file.save(archive_filename, cf)
        report.file_size = cf.size
        report.state = Report.States.DONE
        report.save()
