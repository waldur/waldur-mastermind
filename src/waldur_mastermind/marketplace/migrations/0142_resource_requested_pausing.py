# Generated by Django 4.2.14 on 2024-10-14 12:25

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0141_resource_restrict_member_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="resource",
            name="requested_pausing",
            field=models.BooleanField(default=False),
        ),
    ]