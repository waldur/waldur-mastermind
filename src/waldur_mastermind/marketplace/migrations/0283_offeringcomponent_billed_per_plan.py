"""Record on the component itself whether the plan decides how it is billed.

Until now that was answered by asking the plugin registry whether the
component's type is one the plugin provides for the offering type, with a
prefix match bolted on for the OpenStack per-volume-type quotas. So the answer
lived outside the data: a component changed which rule governed it whenever a
plugin changed the types it registers, and the two spellings of the question
could disagree. The flag is set here for exactly the components the registry
would have matched, so nothing resolves differently afterwards.
"""

from django.db import migrations, models

OPENSTACK_TENANT_OFFERING = "OpenStack.Tenant"
VOLUME_TYPE_COMPONENT_PREFIX = "gigabytes_"


def mark_plugin_components(apps, schema_editor):
    from waldur_mastermind.marketplace import plugins

    OfferingComponent = apps.get_model("marketplace", "OfferingComponent")

    for offering_type in {
        row["offering__type"]
        for row in OfferingComponent.objects.values("offering__type")
    }:
        builtin_types = set(plugins.manager.get_component_types(offering_type))
        components = OfferingComponent.objects.filter(offering__type=offering_type)
        if builtin_types:
            components.filter(type__in=builtin_types).update(billed_per_plan=True)
        if offering_type == OPENSTACK_TENANT_OFFERING:
            components.filter(type__startswith=VOLUME_TYPE_COMPONENT_PREFIX).update(
                billed_per_plan=True
            )


def unmark(apps, schema_editor):
    OfferingComponent = apps.get_model("marketplace", "OfferingComponent")
    OfferingComponent.objects.update(billed_per_plan=False)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0282_plan_billing_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="offeringcomponent",
            name="billed_per_plan",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "The plan's billing mode decides how this component is "
                    "billed, and billing_type above is only what it falls back "
                    "to. Set for the components a plugin provides; a component "
                    "the provider adds keeps its own accounting type under "
                    "every plan."
                ),
            ),
        ),
        migrations.RunPython(mark_plugin_components, unmark),
    ]
