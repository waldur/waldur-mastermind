# -*- coding: utf-8 -*-
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
        ('marketplace', '0001_squashed_0093')
    ]

    operations = [
        migrations.RunPython(update_vmware_component)
    ]
