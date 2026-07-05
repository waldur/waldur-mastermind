from django.db import migrations, models


def preserve_per_resource_semantics(apps, schema_editor):
    """The discounts converted to a formula in 0245 came from the previous
    threshold/rate discount, which was evaluated per resource. Keep them on the
    per-resource scope so existing deployments do not silently switch to
    customer-aggregated discounting. New discounts default to per-customer."""
    PlanComponent = apps.get_model("marketplace", "PlanComponent")
    PlanComponent.objects.exclude(discount_formula="").update(
        discount_aggregation="resource"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0245_plancomponent_discount_formula"),
    ]

    operations = [
        migrations.AddField(
            model_name="plancomponent",
            name="discount_aggregation",
            field=models.CharField(
                choices=[
                    ("resource", "Per resource"),
                    ("customer", "Aggregated per customer"),
                ],
                default="customer",
                help_text=(
                    "Whether the volume discount is computed on a single "
                    "resource's usage or aggregated across all of the customer's "
                    "resources of this offering."
                ),
                max_length=10,
            ),
        ),
        migrations.RunPython(
            preserve_per_resource_semantics,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
