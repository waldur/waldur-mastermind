# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('support_invoices', '0002_delete_requestbasedoffering'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('support', '0036_offering_ordering'),
        ('invoices', '0030_json_details_on_genericinvoiceitem'),
        ('marketplace', '0078_fix_plan_component_amount'),
    ]

    operations = []
