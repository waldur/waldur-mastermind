# Generated by Django 1.11.7 on 2018-02-12 13:48
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('waldur_slurm', '0004_increase_precision'),
    ]

    operations = [
        migrations.AddField(
            model_name='allocation',
            name='deposit_limit',
            field=models.DecimalField(decimal_places=0, default=-1, max_digits=6),
        ),
        migrations.AddField(
            model_name='allocation',
            name='deposit_usage',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=8),
        ),
    ]