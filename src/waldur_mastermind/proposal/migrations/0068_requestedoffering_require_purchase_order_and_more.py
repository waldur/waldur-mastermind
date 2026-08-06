from django.db import migrations, models


def seed_require_purchase_order(apps, schema_editor):
    """Give existing call offerings the same default a new one would get.

    RequestedOffering.save seeds the flag from the offering, but only for rows
    created after this migration. Without a backfill an existing call would
    quietly stop requiring a purchase order that its offering does require.
    """
    RequestedOffering = apps.get_model("proposal", "RequestedOffering")
    rows = RequestedOffering.objects.select_related("offering").only(
        "id", "require_purchase_order", "offering__plugin_options"
    )
    to_update = [
        row
        for row in rows.iterator()
        if (row.offering.plugin_options or {}).get("require_purchase_order_upload")
    ]
    for row in to_update:
        row.require_purchase_order = True
    RequestedOffering.objects.bulk_update(to_update, ["require_purchase_order"])


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0067_move_requested_resource_limits_out_of_attributes"),
    ]

    operations = [
        migrations.AddField(
            model_name="requestedoffering",
            name="require_purchase_order",
            field=models.BooleanField(
                default=False,
                help_text="Whether a purchase order must accompany a resource request for this offering before the proposal can be submitted. Defaults to the offering's require_purchase_order_upload, and stays under the call manager's control afterwards.",
            ),
        ),
        migrations.AddField(
            model_name="requestedresource",
            name="attachment",
            field=models.FileField(
                blank=True,
                null=True,
                upload_to="proposal_requested_resource_attachments",
            ),
        ),
        migrations.AddField(
            model_name="requestedresource",
            name="purchase_order_reference",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.RunPython(seed_require_purchase_order, migrations.RunPython.noop),
    ]
