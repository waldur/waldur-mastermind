import netfields.fields
from django.db import migrations, models

import waldur_core.structure.models


def mark_existing_as_portal_scoped(apps, schema_editor):
    """Every pre-existing entry restricted portal sign-in, so say so explicitly.

    ``applies_to_portal`` defaults to False because a network added to reach a
    resource must not silently restrict who can sign in. Rows that predate the
    flag were created under the old rule — any entry restricted sign-in — so
    they are marked True to preserve exactly the access they already granted.
    """
    AccessSubnet = apps.get_model("structure", "AccessSubnet")
    AccessSubnet.objects.update(applies_to_portal=True)


class Migration(migrations.Migration):
    """Give an access subnet an explicit scope, and record staff provenance.

    ``inet`` also loses its model-level /32 validator: staff may now enter wider
    ranges. The single-host rule still applies to everyone else, but it moved to
    the serializer, which is the only layer that knows who is acting. Existing
    rows are all /32 and stay valid.
    """

    dependencies = [
        ("structure", "0078_alter_servicesettings_certificate"),
    ]

    operations = [
        migrations.AddField(
            model_name="accesssubnet",
            name="applies_to_portal",
            field=models.BooleanField(
                default=False,
                help_text="Whether this network may sign in to the portal on "
                "behalf of the organization. Off by default: any portal-scoped "
                "entry restricts sign-in for everyone in the organization.",
            ),
        ),
        migrations.AddField(
            model_name="accesssubnet",
            name="is_staff_managed",
            field=models.BooleanField(
                default=False,
                help_text="Set when staff created the entry. Such entries are "
                "read-only for everyone else, regardless of mask width.",
            ),
        ),
        migrations.AlterField(
            model_name="accesssubnet",
            name="inet",
            field=netfields.fields.CidrAddressField(
                blank=True,
                max_length=43,
                null=True,
                validators=[waldur_core.structure.models.validate_access_subnet_cidr],
            ),
        ),
        migrations.RunPython(
            mark_existing_as_portal_scoped,
            migrations.RunPython.noop,
        ),
    ]
