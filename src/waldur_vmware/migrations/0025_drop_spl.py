from django.db import migrations

from waldur_core.core.migration_utils import build_spl_migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('waldur_vmware', '0024_error_traceback'),
    ]

    operations = build_spl_migrations(('disk', 'port', 'virtualmachine'),)
