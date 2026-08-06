from django.db import migrations

# core.Feature keys the mode is derived from.
CALL_ONLY = "marketplace.call_only"
CALL_MANAGEMENT = "marketplace.show_call_management_functionality"


def derive_mode(apps, schema_editor):
    """Give an existing deployment the SERVICE_ACCESS_MODE matching what it shows.

    The mode replaces a set of booleans that could express combinations nothing
    handled coherently. It is derived from whether a calls section is rendered
    today, not from ``catalogue_only`` — that one survives as its own setting,
    governing whether ordering is possible at all, which is orthogonal to how a
    user reaches services.

        call_only                        -> calls
        call management on               -> both
        neither                          -> marketplace  (no calls nav today)

    Constance stores values outside the ORM, so this writes through its own
    backend rather than a model. Nothing to reverse: the setting simply falls
    back to its "both" default.
    """
    from constance import config

    Feature = apps.get_model("core", "Feature")
    flags = {
        f.key: f.value
        for f in Feature.objects.filter(key__in=[CALL_ONLY, CALL_MANAGEMENT])
    }

    if flags.get(CALL_ONLY):
        mode = "calls"
    elif flags.get(CALL_MANAGEMENT):
        mode = "both"
    else:
        mode = "marketplace"

    config.SERVICE_ACCESS_MODE = mode


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0257_drop_resource_api_key_fingerprint"),
        ("core", "0040_personalaccesstoken_allowed_networks"),
    ]

    operations = [
        migrations.RunPython(derive_mode, migrations.RunPython.noop),
    ]
