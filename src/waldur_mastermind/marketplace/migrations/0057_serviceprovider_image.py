# Generated by Django 2.2.20 on 2021-07-26 10:33

from django.db import migrations, models

import waldur_core.media.models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0056_offering_divisions'),
    ]

    operations = [
        migrations.AddField(
            model_name='serviceprovider',
            name='image',
            field=models.ImageField(
                blank=True,
                null=True,
                upload_to=waldur_core.media.models.get_upload_path,
            ),
        ),
    ]