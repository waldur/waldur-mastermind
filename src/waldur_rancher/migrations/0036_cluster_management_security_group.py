# Generated by Django 2.2.13 on 2021-03-12 10:29

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0022_remove_tenant_extra_configuration'),
        ('waldur_rancher', '0035_drop_spl'),
    ]

    operations = [
        migrations.AddField(
            model_name='cluster',
            name='management_security_group',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='openstack.SecurityGroup',
            ),
        ),
    ]