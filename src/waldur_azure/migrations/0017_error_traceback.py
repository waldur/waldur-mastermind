# Generated by Django 2.2.13 on 2020-10-07 11:12

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('waldur_azure', '0016_extend_description_limits'),
    ]

    operations = [
        migrations.AddField(
            model_name='network',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='networkinterface',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='publicip',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='resourcegroup',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='securitygroup',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='sqldatabase',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='sqlserver',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='storageaccount',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='subnet',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='virtualmachine',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
    ]
