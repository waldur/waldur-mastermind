# Generated by Django 4.2.8 on 2024-02-15 19:51

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0015_call_backend_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="requestedoffering",
            name="description",
            field=models.CharField(
                blank=True, max_length=2000, verbose_name="description"
            ),
        ),
    ]
