# Generated by Django 4.2.8 on 2024-01-09 08:02

import django.core.validators
import django_fsm
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('proposal', '0009_move_review_strategy_from_call_to_round'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='call',
            name='allocation_strategy',
        ),
        migrations.AddField(
            model_name='round',
            name='allocation_date',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='round',
            name='allocation_time',
            field=django_fsm.FSMIntegerField(
                choices=[(1, 'On decision'), (2, 'Fixed date')], default=1
            ),
        ),
        migrations.AddField(
            model_name='round',
            name='deciding_entity',
            field=django_fsm.FSMIntegerField(
                choices=[
                    (1, 'By call manager'),
                    (2, 'Automatic based on review scoring'),
                ],
                default=2,
            ),
        ),
        migrations.AddField(
            model_name='round',
            name='max_allocations',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='round',
            name='minimal_average_scoring',
            field=models.DecimalField(
                blank=True,
                decimal_places=1,
                max_digits=5,
                null=True,
                validators=[django.core.validators.MinValueValidator(0)],
            ),
        ),
    ]