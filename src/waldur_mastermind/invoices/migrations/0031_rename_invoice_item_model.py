# -*- coding: utf-8 -*-
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0001_squashed_0030'),
    ]

    operations = [
        migrations.RenameModel('GenericInvoiceItem', 'InvoiceItem')
    ]
