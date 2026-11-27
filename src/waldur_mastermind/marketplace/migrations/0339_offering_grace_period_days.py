from django.db import migrations

# disable_grace_period true is grace_period_days 0. Copy it onto the key the
# offering clock reads first, and keep the boolean. Homeport still shows that
# toggle, and the two agree. The new key wins when a later edit sets both.


def copy_disabled_grace_period(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    offerings = Offering.objects.filter(
        plugin_options__disable_grace_period=True
    ).iterator()
    for offering in offerings:
        options = dict(offering.plugin_options or {})
        if options.get("grace_period_days") is None:
            options["grace_period_days"] = 0
        offering.plugin_options = options
        offering.save(update_fields=["plugin_options"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0338_merge_k8s_option_types"),
    ]

    operations = [
        migrations.RunPython(copy_disabled_grace_period, migrations.RunPython.noop),
    ]
