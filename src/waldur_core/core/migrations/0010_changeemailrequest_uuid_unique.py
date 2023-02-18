from django.db import migrations

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0009_changeemailrequest_uuid_populate'),
    ]

    operations = [
        migrations.AlterField(
            model_name='changeemailrequest',
            name='uuid',
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
