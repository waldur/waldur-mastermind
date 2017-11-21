# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


def add_openstack_component_details_to_tenant(apps, schema_editor):
    OpenStackPackage = apps.get_model('packages', 'OpenStackPackage')

    for package in OpenStackPackage.objects.all():
        for component in package.template.components.all():
            package.tenant.extra_configuration[component.type] = component.amount

        package.tenant.save()


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0010_packagetemplate_protect'),
    ]

    operations = [
        migrations.RunPython(add_openstack_component_details_to_tenant),
    ]
