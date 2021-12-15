from django.db import migrations

from waldur_core.core.migration_utils import build_spl_migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('waldur_slurm', '0022_allocation_user_usage_mandatory_fields'),
    ]

    operations = build_spl_migrations(('allocation',),)
