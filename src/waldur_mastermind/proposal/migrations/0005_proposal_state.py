# Generated by Django 3.2.20 on 2023-12-10 21:49

import django.db.models.deletion
import django_fsm
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0004_proposal"),
    ]

    operations = [
        migrations.AlterField(
            model_name="proposal",
            name="round",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="proposal.round"
            ),
        ),
        migrations.AlterField(
            model_name="proposal",
            name="state",
            field=django_fsm.FSMIntegerField(
                choices=[
                    (1, "Draft"),
                    (2, "Submitted"),
                    (3, "In review"),
                    (4, "In revision"),
                    (5, "Accepted"),
                    (6, "Rejected"),
                    (7, "Canceled"),
                ],
                default=1,
            ),
        ),
    ]
