# Generated by Django 2.2.24 on 2021-12-10 14:33

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('waldur_freeipa', '0002_decrease_username_length'),
    ]

    operations = [
        migrations.AlterField(
            model_name='profile',
            name='is_active',
            field=models.BooleanField(default=False, verbose_name='active'),
        ),
    ]
