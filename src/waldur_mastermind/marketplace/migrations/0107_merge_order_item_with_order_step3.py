# Generated by Django 3.2.20 on 2023-11-03 08:46

import django.db.models.deletion
import django_fsm
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('structure', '0040_useragreement_uuid'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('marketplace', '0106_merge_order_item_with_order_step2'),
        ('marketplace_script', '0004_remove_dryrun_order'),
    ]

    operations = [
        migrations.AlterField(
            model_name='orderitem',
            name='created_by',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='core.user',
            ),
        ),
        migrations.AlterField(
            model_name='orderitem',
            name='project',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to='structure.project'
            ),
        ),
        migrations.RemoveField(
            model_name='orderitem',
            name='order',
        ),
        migrations.AlterField(
            model_name='orderitem',
            name='state',
            field=django_fsm.FSMIntegerField(
                choices=[
                    (1, 'pending-consumer'),
                    (7, 'pending-provider'),
                    (2, 'executing'),
                    (3, 'done'),
                    (4, 'erred'),
                    (5, 'canceled'),
                    (6, 'rejected'),
                ],
                default=1,
            ),
        ),
        migrations.DeleteModel(
            name='Order',
        ),
    ]