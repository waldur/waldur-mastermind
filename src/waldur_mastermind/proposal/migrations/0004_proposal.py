# Generated by Django 3.2.20 on 2023-12-07 11:18

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("structure", "0040_useragreement_uuid"),
        ("proposal", "0003_requested_offering"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="proposal",
            name="duration_requested",
        ),
        migrations.RemoveField(
            model_name="proposal",
            name="resource_usage",
        ),
        migrations.AddField(
            model_name="proposal",
            name="approved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="created_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="proposal",
            name="duration_in_days",
            field=models.PositiveIntegerField(
                help_text="Duration in days after provisioning of resources.",
                null=True,
                blank=True,
            ),
        ),
        migrations.AlterField(
            model_name="proposal",
            name="project",
            field=models.ForeignKey(
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="structure.project",
            ),
        ),
    ]
