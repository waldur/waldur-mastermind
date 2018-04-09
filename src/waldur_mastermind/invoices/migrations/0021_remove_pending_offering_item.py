from __future__ import unicode_literals

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def migrate_data(apps, schema_editor):
    from waldur_mastermind.invoices.models import OfferingItem
    from waldur_mastermind.support.models import Offering

    OfferingItem.objects.filter(offering__state=Offering.States.REQUESTED).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('invoices', '0020_migrate_payment_details_data'),
    ]

    operations = [
        migrations.RunPython(migrate_data, reverse_code=migrations.RunPython.noop),
    ]
