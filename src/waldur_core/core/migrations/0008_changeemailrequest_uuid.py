from django.db import migrations

import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_changeemailrequest'),
    ]

    operations = [
        migrations.AddField(
            model_name='changeemailrequest',
            name='uuid',
            field=waldur_core.core.fields.UUIDField(null=True),
        ),
    ]
