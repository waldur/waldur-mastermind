# Generated by Django 2.2.13 on 2020-10-09 21:15

import django_fsm
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0006_router'),
    ]

    operations = [
        migrations.AddField(
            model_name='router',
            name='error_message',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='router',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='router',
            name='routes',
            field=waldur_core.core.fields.JSONField(default=list),
        ),
        migrations.AddField(
            model_name='router',
            name='state',
            field=django_fsm.FSMIntegerField(
                choices=[
                    (5, 'Creation Scheduled'),
                    (6, 'Creating'),
                    (1, 'Update Scheduled'),
                    (2, 'Updating'),
                    (7, 'Deletion Scheduled'),
                    (8, 'Deleting'),
                    (3, 'OK'),
                    (4, 'Erred'),
                ],
                default=5,
            ),
        ),
    ]