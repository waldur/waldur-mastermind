# Generated by Django 4.2.10 on 2024-07-23 22:33

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("policy", "0004_customerestimatedcostpolicy"),
    ]

    operations = [
        migrations.RenameField(
            model_name="customerestimatedcostpolicy",
            old_name="customer",
            new_name="scope",
        ),
        migrations.RenameField(
            model_name="projectestimatedcostpolicy",
            old_name="project",
            new_name="scope",
        ),
    ]
