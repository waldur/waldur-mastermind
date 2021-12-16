from django.db import migrations

from waldur_core.core.migration_utils import build_spl_migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('openstack_tenant', '0020_create_or_update_security_group_rules'),
    ]

    operations = build_spl_migrations(
        (
            'backup',
            'backupschedule',
            'instance',
            'snapshot',
            'snapshotschedule',
            'volume',
        ),
    ) + [
        migrations.AlterUniqueTogether(
            name='instance', unique_together={('service_settings', 'backend_id')},
        ),
        migrations.AlterUniqueTogether(
            name='snapshot', unique_together={('service_settings', 'backend_id')},
        ),
        migrations.AlterUniqueTogether(
            name='volume', unique_together={('service_settings', 'backend_id')},
        ),
    ]
