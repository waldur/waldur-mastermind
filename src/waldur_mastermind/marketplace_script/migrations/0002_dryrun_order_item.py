# Generated by Django 3.2.20 on 2023-11-03 09:04

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0105_merge_order_item_with_order_step1"),
        ("marketplace_script", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="dryrun",
            name="order_item",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="marketplace.orderitem",
            ),
        ),
    ]
