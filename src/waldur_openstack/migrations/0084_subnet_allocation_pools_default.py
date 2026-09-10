"""`SubNet.allocation_pools` is a list, and some rows hold `{}` (#390).

The field's default was `dict`, so every subnet created without an explicit pool
stored `{}` -- a value the serializer, the OpenAPI schema and every generated
client declare as an array. The default is corrected here, and the rows already
carrying the wrong shape are turned into empty lists; the next pull fills in the
real pool, and until then an empty list is at least the type the API promises.
"""

import logging

from django.db import migrations

import waldur_core.core.fields

logger = logging.getLogger(__name__)


def normalise_allocation_pools(apps, schema_editor):
    SubNet = apps.get_model("openstack", "subnet")
    stale = []
    for subnet in SubNet.objects.all().only("id", "allocation_pools").iterator():
        if not isinstance(subnet.allocation_pools, list):
            subnet.allocation_pools = []
            stale.append(subnet)
    if stale:
        SubNet.objects.bulk_update(stale, ["allocation_pools"], batch_size=500)
    logger.info("Normalised allocation_pools on %s subnet(s)", len(stale))


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0083_subnet_router"),
    ]

    operations = [
        migrations.AlterField(
            model_name="subnet",
            name="allocation_pools",
            field=waldur_core.core.fields.JSONField(
                blank=True,
                default=list,
                help_text="List of IP ranges available for allocation in this subnet",
            ),
        ),
        migrations.RunPython(
            normalise_allocation_pools, migrations.RunPython.noop, elidable=True
        ),
    ]
