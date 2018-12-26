import cStringIO
import datetime
import os
import tarfile

from django.apps import apps
from django.core.files.base import ContentFile

from waldur_core.core.utils import datetime_to_timestamp
from waldur_core.logging.loggers import LoggableMixin


def get_loggable_models():
    return [model for model in apps.get_models() if issubclass(model, LoggableMixin)]


def get_scope_types_mapping():
    return {str(m._meta): m for m in get_loggable_models()}


def get_reverse_scope_types_mapping():
    return {m: str(m._meta) for m in get_loggable_models()}


def create_report_archive(log_directory, interval):
    """
    Create tar.gz archive from files in directory filtered by time delta.
    :param log_directory: directory with log files, for example, /var/log/waldur/
    :param interval: time delta, for example, datetime.timedelta(days=7)
    files older that specified interval are filtered out
    :return: ContentFile with gzipped archive
    """
    today = datetime.datetime.today()
    cutoff = datetime_to_timestamp(today - interval)

    log_filenames = []
    log_dir = log_directory
    for log_file in os.listdir(log_dir):
        full_path = os.path.join(log_dir, log_file)
        stat = os.stat(full_path)
        if stat.st_mtime > cutoff:
            log_filenames.append(full_path)

    stream = cStringIO.StringIO()

    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for filename in log_filenames:
            archive.add(filename)

    return ContentFile(stream.getvalue())
