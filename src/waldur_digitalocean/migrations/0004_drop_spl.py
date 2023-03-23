import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('waldur_digitalocean', '0003_droplet_error_traceback'),
        ('structure', '0001_squashed_0036'),
    ]

    operations = [
        migrations.AddField(
            model_name='droplet',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.Project',
            ),
        ),
        migrations.AddField(
            model_name='droplet',
            name='service_settings',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='structure.ServiceSettings',
            ),
        ),
    ]
