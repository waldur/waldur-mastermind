# Generated by Django 1.11.20 on 2019-04-22 09:32
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0008_customer_division'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='is_removed',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterModelOptions(
            name='project', options={'base_manager_name': 'objects'},
        ),
    ]