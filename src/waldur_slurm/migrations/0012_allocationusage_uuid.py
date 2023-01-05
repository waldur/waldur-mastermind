from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_slurm', '0011_change_ram_usage_to_mb'),
    ]

    operations = [
        migrations.AddField(
            model_name='allocationusage',
            name='uuid',
            field=models.UUIDField(null=True),
        ),
        migrations.AlterField(
            model_name='allocationusage',
            name='uuid',
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
