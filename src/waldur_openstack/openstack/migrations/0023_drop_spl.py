from django.db import migrations

from waldur_core.core.migration_utils import build_spl_migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('openstack', '0022_remove_tenant_extra_configuration'),
    ]

    operations = build_spl_migrations(
        (
            'floatingip',
            'network',
            'port',
            'router',
            'securitygroup',
            'subnet',
            'tenant',
        ),
    ) + [
        migrations.AlterUniqueTogether(
            name='tenant',
            unique_together={('service_settings', 'backend_id')},
        ),
    ]
