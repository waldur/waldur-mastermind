import django.contrib.postgres.fields.jsonb
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0037_resource_backend_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='resource',
            name='report',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True),
        ),
    ]
