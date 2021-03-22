from django.db import migrations

from waldur_core.core.migration_utils import build_spl_migrations


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('waldur_azure', '0017_error_traceback'),
    ]

    operations = build_spl_migrations(
        'waldur_azure',
        'AzureService',
        'AzureServiceProjectLink',
        (
            'network',
            'networkinterface',
            'publicip',
            'resourcegroup',
            'securitygroup',
            'sqldatabase',
            'sqlserver',
            'storageaccount',
            'subnet',
            'virtualmachine',
        ),
    )
