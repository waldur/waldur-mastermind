# Generated by Django 3.2.20 on 2023-11-20 23:34

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0110_provider_reviewed_by'),
    ]

    operations = [
        migrations.RenameField(
            model_name='order',
            old_name='approved_at',
            new_name='consumer_reviewed_at',
        ),
        migrations.RenameField(
            model_name='order',
            old_name='approved_by',
            new_name='consumer_reviewed_by',
        ),
    ]