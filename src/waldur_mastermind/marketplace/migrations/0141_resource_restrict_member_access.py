# Generated by Django 4.2.14 on 2024-10-04 12:26

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0140_categorycolumn_uuid"),
    ]

    operations = [
        migrations.AddField(
            model_name="resource",
            name="restrict_member_access",
            field=models.BooleanField(default=False),
        ),
    ]
