from django.db import migrations

LEGACY_KEY = "customer.billing_mode"
CURRENT_KEY = "customer.show_invoices_with_vat"


def carry_over_billing_mode(apps, schema_editor):
    # The flag shipped in 8.1.5-rc.1 as "customer.billing_mode" and was
    # renamed so it cannot be mistaken for a plan's billing mode. Carry an
    # explicit value over unless the new key is already set, then drop the
    # legacy row so it cannot be mistaken for the live setting.
    Feature = apps.get_model("core", "Feature")
    legacy = Feature.objects.filter(key=LEGACY_KEY).first()
    if legacy is None:
        return
    Feature.objects.get_or_create(key=CURRENT_KEY, defaults={"value": legacy.value})
    legacy.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0058_reauthentication_widen_pat_networks"),
    ]

    operations = [
        migrations.RunPython(
            carry_over_billing_mode, reverse_code=migrations.RunPython.noop
        ),
    ]
