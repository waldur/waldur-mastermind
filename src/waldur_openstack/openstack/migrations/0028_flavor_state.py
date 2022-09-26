# Generated by Django 3.2.13 on 2022-07-14 12:25

import django_fsm
from django.db import migrations, models


def set_flavor_state(apps, schema_editor):
    Flavor = apps.get_model('openstack', 'Flavor')
    Flavor.objects.update(state=3)


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0027_alter_servergroup_policy'),
    ]

    operations = [
        migrations.AddField(
            model_name='flavor',
            name='error_message',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='flavor',
            name='error_traceback',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='flavor',
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
        migrations.AlterUniqueTogether(
            name='flavor',
            unique_together=set(),
        ),
        migrations.RunPython(set_flavor_state),
    ]