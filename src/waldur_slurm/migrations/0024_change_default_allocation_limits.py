# Generated by Django 2.2.19 on 2021-03-30 13:55

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('waldur_slurm', '0023_drop_spl'),
    ]

    operations = [
        migrations.AlterField(
            model_name='allocation',
            name='cpu_limit',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name='allocation',
            name='gpu_limit',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name='allocation',
            name='ram_limit',
            field=models.BigIntegerField(default=0),
        ),
    ]
