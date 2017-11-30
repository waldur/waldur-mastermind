# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.core.fields


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0003_paymentdetails_default_tax_percent'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentdetails',
            name='uuid',
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
