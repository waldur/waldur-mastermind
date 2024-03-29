# Generated by Django 4.2.10 on 2024-03-26 08:13

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0014_unify_log"),
        ("proposal", "0027_proposal_member_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="call",
            name="default_project_role",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="permissions.role",
            ),
        ),
    ]
