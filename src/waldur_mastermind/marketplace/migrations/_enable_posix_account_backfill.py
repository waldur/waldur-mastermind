"""Backfill the ``enable_posix_account`` plugin option on existing offerings.

The option was added with a default of ``true`` and unset is already treated
as enabled, so this changes no behaviour — it only stamps the key onto
offerings created before the option existed, so the toggle renders correctly
in the UI.

Scope: only offerings that **manage user/POSIX accounts** — i.e. those whose
``service_provider_can_create_offering_user`` is enabled. Offerings that never
create offering users (and therefore never manage a POSIX/LDAP account) are
left untouched, so the key is not stamped onto unrelated offering types.

Logic lives here (underscore prefix keeps it out of the migration loader) so it
can be unit-tested.
"""


def backfill_enable_posix_account(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    for offering in (
        Offering.objects.filter(
            plugin_options__service_provider_can_create_offering_user=True
        )
        .exclude(plugin_options__has_key="enable_posix_account")
        .iterator()
    ):
        plugin_options = offering.plugin_options or {}
        plugin_options["enable_posix_account"] = True
        offering.plugin_options = plugin_options
        offering.save(update_fields=["plugin_options"])
