# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

from waldur_core.structure.permissions import _get_project


def set_invoice_item_project(apps, schema_editor):
    OpenStackItem = apps.get_model('invoices', 'OpenStackItem')
    OfferingItem = apps.get_model('invoices', 'OfferingItem')

    for item in OpenStackItem.objects.all().exclude(package__isnull=True):
        project = _get_project(item.package)
        item.project_name = project.name
        item.project_uuid = project.uuid.hex
        item.save(update_fields=['project_name', 'project_uuid'])

    for item in OfferingItem.objects.all().exclude(offering__isnull=True):
        item.project_name = item.offering.project.name
        item.project_uuid = item.offering.project.uuid.hex
        item.save(update_fields=['project_name', 'project_uuid'])


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0012_add_project_to_invoice_item'),
    ]

    operations = [
        migrations.RunPython(set_invoice_item_project),
    ]
