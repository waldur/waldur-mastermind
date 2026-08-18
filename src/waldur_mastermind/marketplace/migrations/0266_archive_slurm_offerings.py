from django.db import migrations

# Offerings of type SlurmInvoices.SlurmPackage outlive the plugin that served
# them: removing waldur_mastermind.marketplace_slurm unregisters the create and
# delete processors, but the Offering rows stay in the database.
#
# Left ACTIVE they are still orderable. The order would be accepted, then fail
# in process_order() with "Skipping order processing because processor is not
# found." and leave both order and resource ERRED.
#
# Archiving closes that door cleanly: Order.validate_offering() requires
# state == ACTIVE and rejects anything else with "Offering is not available.",
# and validate_offering_update() blocks edits to archived offerings. Archived
# offerings remain visible in the catalogue, so existing customers keep their
# history.
#
# Deliberately NOT deleting these offerings: Resource.offering is CASCADE (via
# SafeAttributesMixin), so a delete would take every historical SLURM resource
# and its orders with it. InvoiceItem.resource is SET_NULL, so invoice totals
# would survive -- but the link between a charge and the thing charged would
# not.

SLURM_OFFERING = "SlurmInvoices.SlurmPackage"

ARCHIVED = 4


def archive_slurm_offerings(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    stale = Offering.objects.filter(type=SLURM_OFFERING).exclude(state=ARCHIVED)
    for offering in stale:
        print(
            f"Archiving offering {offering.uuid} ({offering.name}): "
            f"the {SLURM_OFFERING} plugin has been removed."
        )
    stale.update(state=ARCHIVED)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0265_drop_slurm_tables"),
    ]

    operations = [
        # Reverse is a noop: the previous per-offering state is not recorded, and
        # re-activating an offering with no processor would only re-open the hole.
        migrations.RunPython(archive_slurm_offerings, migrations.RunPython.noop),
    ]
