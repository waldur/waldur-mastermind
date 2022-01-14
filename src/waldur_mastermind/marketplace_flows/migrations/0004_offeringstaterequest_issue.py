# Generated by Django 2.2.24 on 2021-10-30 07:18

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0015_fill_attachment_mime_type'),
        ('marketplace_flows', '0003_long_project_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='offeringstaterequest',
            name='issue',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='support.Issue',
            ),
        ),
    ]