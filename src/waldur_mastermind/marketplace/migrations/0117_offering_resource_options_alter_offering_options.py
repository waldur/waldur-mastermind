# Generated by Django 4.2.8 on 2024-01-09 08:47

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0116_plancomponent_future_price"),
    ]

    operations = [
        migrations.AddField(
            model_name="offering",
            name="resource_options",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Fields describing resource report form.",
            ),
        ),
        migrations.AlterField(
            model_name="offering",
            name="options",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Fields describing resource provision form.",
            ),
        ),
    ]
