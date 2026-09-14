from django.db import migrations

# Resource limit change requests became an opt-in offering feature: they are
# accepted only when plugin_options.enable_resource_limit_change_requests is set.
# Offerings that have received requests (in any state) keep the feature, so a
# deployment using it sees no change; every other offering starts with it off.
#
# A child offering is shown, and checked, with its parent's plugin options, so
# the option is set on the parent in that case.
#
# Rows are changed through the historical model with queryset updates, so no
# signal handlers run. Offerings that already carry the key are left as they are.
#
# Reverse is a noop: the option is ignored by the code this migration precedes.

OPTION = "enable_resource_limit_change_requests"


def enable_limit_change_requests_where_used(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    ResourceLimitChangeRequest = apps.get_model(
        "marketplace", "ResourceLimitChangeRequest"
    )
    used = Offering.objects.filter(
        id__in=ResourceLimitChangeRequest.objects.values("resource__offering_id")
    ).values_list("id", "parent_id")
    target_ids = {parent_id or offering_id for offering_id, parent_id in used}
    enabled = 0
    for offering in Offering.objects.filter(id__in=target_ids):
        plugin_options = offering.plugin_options or {}
        if OPTION in plugin_options:
            continue
        Offering.objects.filter(pk=offering.pk).update(
            plugin_options={**plugin_options, OPTION: True}
        )
        enabled += 1
    if enabled:
        print(f"Enabled limit change requests on {enabled} offering(s) using them.")


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0290_anonymized_username_from_uid"),
    ]

    operations = [
        migrations.RunPython(
            enable_limit_change_requests_where_used, migrations.RunPython.noop
        ),
    ]
