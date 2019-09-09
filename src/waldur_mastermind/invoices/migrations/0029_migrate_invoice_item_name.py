# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        # See also: https://code.djangoproject.com/ticket/24303
        ('contenttypes', '0002_remove_content_type_name'),
        ('invoices', '0028_delete_offering_and_openstack_item'),
    ]

    operations = []
