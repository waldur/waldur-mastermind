# Generated by Django 2.2.13 on 2020-10-07 11:12

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0003_extend_organization'),
    ]

    operations = [
        migrations.AddField(
            model_name='invitation',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
    ]
