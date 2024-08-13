# Generated by Django 4.2.14 on 2024-08-05 08:04

import django_fsm
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0006_adjust_invitation_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="invitation",
            name="execution_state",
            field=django_fsm.FSMField(
                choices=[
                    ("Scheduled", "Scheduled"),
                    ("Processing", "Processing"),
                    ("OK", "OK"),
                    ("Erred", "Erred"),
                ],
                default="Scheduled",
                max_length=50,
            ),
        ),
    ]