from django.db import migrations, models


def convert_discounts_to_formula(apps, schema_editor):
    """Convert the flat threshold/rate volume discount into the equivalent
    formula ``<rate> if usage >= <threshold> else 0``."""
    PlanComponent = apps.get_model("marketplace", "PlanComponent")
    for component in PlanComponent.objects.exclude(discount_rate=None).exclude(
        discount_threshold=None
    ):
        component.discount_formula = f"{component.discount_rate} if usage >= {component.discount_threshold} else 0"
        component.save(update_fields=["discount_formula"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0244_offeringuserattributeconfig_expose_organization_address"),
    ]

    operations = [
        migrations.AddField(
            model_name="plancomponent",
            name="discount_formula",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Volume discount formula evaluated with the billed quantity "
                    "bound to `usage`; returns a discount percentage (clamped to "
                    "0-100). Empty means no discount. Example: '10 if usage >= "
                    "100 else 0'."
                ),
            ),
        ),
        migrations.RunPython(
            convert_discounts_to_formula,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="plancomponent",
            name="discount_threshold",
        ),
        migrations.RemoveField(
            model_name="plancomponent",
            name="discount_rate",
        ),
    ]
