# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


def migrate_items(apps, schema_editor):
    OpenStackItem = apps.get_model('invoices', 'OpenStackItem')
    OfferingItem = apps.get_model('invoices', 'OfferingItem')
    GenericInvoiceItem = apps.get_model('invoices', 'GenericInvoiceItem')

    for item in OpenStackItem.objects.all():
        generic_item = GenericInvoiceItem.objects.create(
            invoice=item.invoice,
            details=item.package_details,
            start=item.start,
            end=item.end,
            project=item.project,
            project_name=item.project_name,
            project_uuid=item.project_uuid,
            unit_price=item.unit_price,
            unit=item.unit,
            product_code=item.product_code,
            article_code=item.article_code
        )
        if item.package:
            generic_item.scope = item.package
            generic_item.save()

    for item in OfferingItem.objects.all():
        generic_item = GenericInvoiceItem.objects.create(
            invoice=item.invoice,
            details=item.offering_details,
            start=item.start,
            end=item.end,
            project=item.project,
            project_name=item.project_name,
            project_uuid=item.project_uuid,
            unit_price=item.unit_price,
            unit=item.unit,
            product_code=item.product_code,
            article_code=item.article_code
        )
        if item.offering:
            generic_item.scope = item.offering
            generic_item.save()


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0026_invoice__file'),
    ]

    operations = [
        migrations.RunPython(migrate_items),
    ]
