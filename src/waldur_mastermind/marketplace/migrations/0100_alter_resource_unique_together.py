# Generated by Django 3.2.20 on 2023-10-23 23:22

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('marketplace', '0099_offering_getting_started'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='resource',
            unique_together={('content_type', 'object_id')},
        ),
    ]