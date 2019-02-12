from __future__ import unicode_literals

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def migrate_data(apps, schema_editor):
    OfferingItem = apps.get_model('invoices', 'OfferingItem')

    class States(object):
        REQUESTED = 'requested'
        OK = 'ok'
        TERMINATED = 'terminated'

    OfferingItem.objects.filter(offering__state=States.REQUESTED).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('invoices', '0020_migrate_payment_details_data'),
    ]

    operations = [
        migrations.RunPython(migrate_data, reverse_code=migrations.RunPython.noop),
    ]
