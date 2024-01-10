# Generated by Django 3.2.16 on 2022-12-02 12:24

import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0004_messagetemplate"),
        ("marketplace", "0001_squashed_0076"),
        ("booking", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="BusySlot",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("start", models.DateTimeField()),
                ("end", models.DateTimeField()),
                ("backend_id", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "offering",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="marketplace.offering",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
    ]
