# Generated by Django 4.2.10 on 2024-07-26 09:49

from django.db import migrations

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0134_set_uuid_to_offeringuser"),
    ]

    operations = [
        migrations.AlterField(
            model_name="offeringuser",
            name="uuid",
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
