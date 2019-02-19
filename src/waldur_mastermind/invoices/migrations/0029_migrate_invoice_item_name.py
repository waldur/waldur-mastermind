# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


def migrate_items(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    GenericInvoiceItem = apps.get_model('invoices', 'GenericInvoiceItem')

    Offering = apps.get_model('support', 'Offering')
    OpenStackPackage = apps.get_model('packages', 'OpenStackPackage')

    offering_ct = ContentType.objects.get_for_model(Offering)
    package_ct = ContentType.objects.get_for_model(OpenStackPackage)

    for item in GenericInvoiceItem.objects.filter(object_id=None):
        if item.details.get('tenant_name') and item.details.get('tenant_uuid'):
            item.content_type = package_ct
            item.details.update({
                'name': item.details['tenant_name'],
                'scope_uuid': item.details['tenant_uuid'],
            })
            item.save()

        elif item.details.get('offering_name') and item.details.get('offering_uuid'):
            item.content_type = offering_ct
            item.details.update({
                'name': item.details['offering_name'],
                'scope_uuid': item.details['offering_uuid'],
            })
            item.save()


class Migration(migrations.Migration):

    dependencies = [
        # See also: https://code.djangoproject.com/ticket/24303
        ('contenttypes', '0002_remove_content_type_name'),
        ('invoices', '0028_delete_offering_and_openstack_item'),
    ]

    operations = [
        migrations.RunPython(migrate_items),
    ]
