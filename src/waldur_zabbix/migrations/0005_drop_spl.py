from django.db import migrations

from waldur_core.core.migration_utils import build_spl_migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('waldur_zabbix', '0004_error_traceback'),
    ]

    operations = build_spl_migrations(
        'waldur_zabbix',
        'ZabbixService',
        'ZabbixServiceProjectLink',
        ('host', 'itservice'),
    )
