# Generated by Django 2.2.13 on 2020-10-13 16:53

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0008_subnet_host_routes'),
    ]

    operations = [
        migrations.AddField(
            model_name='subnet',
            name='is_connected',
            field=models.BooleanField(
                default=True,
                help_text='Is subnet connected to the default tenant router.',
            ),
        ),
    ]