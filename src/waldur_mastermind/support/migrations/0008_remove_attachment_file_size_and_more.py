# Generated by Django 4.2.16 on 2024-10-16 16:43

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0007_alter_supportuser_unique_together"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="attachment",
            name="file_size",
        ),
        migrations.RemoveField(
            model_name="attachment",
            name="mime_type",
        ),
        migrations.RemoveField(
            model_name="attachment",
            name="thumbnail",
        ),
    ]
