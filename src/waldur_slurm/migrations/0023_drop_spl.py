import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('waldur_slurm', '0022_allocation_user_usage_mandatory_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='allocation',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.Project',
            ),
        ),
        migrations.AddField(
            model_name='allocation',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.ServiceSettings',
            ),
        ),
    ]
