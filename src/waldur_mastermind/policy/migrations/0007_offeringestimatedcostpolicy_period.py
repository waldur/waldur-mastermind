# Generated by Django 4.2.10 on 2024-07-26 07:01

import django_fsm
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("policy", "0006_offeringestimatedcostpolicy"),
    ]

    operations = [
        migrations.AddField(
            model_name="offeringestimatedcostpolicy",
            name="period",
            field=django_fsm.FSMIntegerField(
                choices=[(1, "Total"), (2, "1 month"), (3, "3 month"), (4, "12 month")],
                default=2,
            ),
        ),
    ]
