# Generated by Django 3.2.20 on 2023-11-03 09:07

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace_script", "0003_dryrun_order_item_fill"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="dryrun",
            name="order",
        ),
    ]
