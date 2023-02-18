# Generated by Django 1.11.18 on 2019-01-29 15:34
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('waldur_azure', '0004_storageaccount'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='image',
            options={'ordering': ['publisher', 'name', 'sku']},
        ),
        migrations.AlterModelOptions(
            name='location',
            options={'ordering': ['name']},
        ),
        migrations.AlterUniqueTogether(
            name='image',
            unique_together=set([]),
        ),
        migrations.AlterModelOptions(
            name='size',
            options={'ordering': ['number_of_cores', 'memory_in_mb']},
        ),
        migrations.AlterUniqueTogether(
            name='image',
            unique_together=set([('settings', 'backend_id')]),
        ),
        migrations.RemoveField(
            model_name='image',
            name='offer',
        ),
    ]
