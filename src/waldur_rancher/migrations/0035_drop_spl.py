from django.db import migrations

from waldur_core.core.migration_utils import build_spl_migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('waldur_rancher', '0034_delete_catalogs_without_scope'),
    ]

    operations = build_spl_migrations(
        (
            'application',
            'cluster',
            'ingress',
            'service',
        ),
    )
