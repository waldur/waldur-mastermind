# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models


def set_tenant_extra_configuration(apps, schema_editor):
    OpenStackPackage = apps.get_model('packages', 'OpenStackPackage')

    for package in OpenStackPackage.objects.all():
        package.tenant.extra_configuration = {
            'package_name': package.template.name,
            'package_uuid': package.template.uuid.hex,
            'package_category': package.template.get_category_display(),
        }
        package.tenant.save()


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0008_package_component_type'),
    ]

    operations = [
        migrations.RunPython(set_tenant_extra_configuration),
    ]
