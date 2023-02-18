import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('waldur_jira', '0023_error_traceback'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.Project',
            ),
        ),
        migrations.AddField(
            model_name='project',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.ServiceSettings',
            ),
        ),
    ]
