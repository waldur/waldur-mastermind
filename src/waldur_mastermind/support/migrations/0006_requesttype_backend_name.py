# Generated by Django 4.2.8 on 2024-01-31 15:06

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0005_fix_issue_order_item"),
    ]

    operations = [
        migrations.AddField(
            model_name="requesttype",
            name="backend_name",
            field=models.CharField(blank=True, default=None, max_length=255, null=True),
        ),
    ]
