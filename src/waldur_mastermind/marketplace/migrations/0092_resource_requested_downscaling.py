# Generated by Django 3.2.18 on 2023-07-21 13:00

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0091_alter_offering_access_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="resource",
            name="requested_downscaling",
            field=models.BooleanField(default=False),
        ),
    ]
