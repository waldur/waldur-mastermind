# Generated by Django 3.2.20 on 2023-11-13 09:17

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0103_offering_integration_guide"),
    ]

    operations = [
        migrations.AddField(
            model_name="category",
            name="description_cs",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="category",
            name="title_cs",
            field=models.CharField(max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="categorygroup",
            name="description_cs",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="categorygroup",
            name="title_cs",
            field=models.CharField(max_length=255, null=True),
        ),
    ]
