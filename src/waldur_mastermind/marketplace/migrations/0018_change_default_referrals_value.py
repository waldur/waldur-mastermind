from django.db import migrations

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0017_change_default_disable_quotas'),
    ]

    operations = [
        migrations.AlterField(
            model_name='offering',
            name='referrals',
            field=waldur_core.core.fields.JSONField(
                blank=True, default=list, help_text='Referrals list for the current DOI'
            ),
        ),
    ]
