import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0038_cleanup_agreement_number_placeholder'),
        ('waldur_vmware', '0024_error_traceback'),
    ]

    operations = [
        migrations.AddField(
            model_name='disk',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.Project',
            ),
        ),
        migrations.AddField(
            model_name='disk',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.ServiceSettings',
            ),
        ),
        migrations.AddField(
            model_name='port',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.Project',
            ),
        ),
        migrations.AddField(
            model_name='port',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.ServiceSettings',
            ),
        ),
        migrations.AddField(
            model_name='virtualmachine',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.Project',
            ),
        ),
        migrations.AddField(
            model_name='virtualmachine',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.ServiceSettings',
            ),
        ),
    ]
