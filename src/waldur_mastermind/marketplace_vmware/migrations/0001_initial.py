# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


def update_vmware_component(apps, schema_editor):
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')
    OfferingComponent.objects.filter(
        offering__type='VMware.VirtualMachine',
        type='cpu',
    ).update(
        measured_unit='vCPU'
    )


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0089_make_billing_period_non_nullable')
    ]

    operations = [
        migrations.RunPython(update_vmware_component)
    ]
