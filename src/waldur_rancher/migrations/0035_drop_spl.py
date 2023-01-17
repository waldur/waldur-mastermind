import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0021_project_backend_id'),
        ('waldur_rancher', '0034_delete_catalogs_without_scope'),
    ]

    operations = [
        migrations.AddField(
            model_name='application',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.Project',
            ),
        ),
        migrations.AddField(
            model_name='application',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.servicesettings',
            ),
        ),
        migrations.AddField(
            model_name='cluster',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.project',
            ),
        ),
        migrations.AddField(
            model_name='cluster',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.servicesettings',
            ),
        ),
        migrations.AddField(
            model_name='ingress',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.project',
            ),
        ),
        migrations.AddField(
            model_name='ingress',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.servicesettings',
            ),
        ),
        migrations.AddField(
            model_name='service',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.project',
            ),
        ),
        migrations.AddField(
            model_name='service',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.ServiceSettings',
            ),
        ),
    ]
