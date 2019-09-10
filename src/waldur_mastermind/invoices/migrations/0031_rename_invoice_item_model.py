# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0030_json_details_on_genericinvoiceitem'),
    ]

    operations = [
        migrations.RenameModel('GenericInvoiceItem', 'InvoiceItem')
    ]
