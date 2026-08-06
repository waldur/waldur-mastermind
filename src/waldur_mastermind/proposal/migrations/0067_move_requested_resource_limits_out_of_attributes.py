from django.db import migrations


def move_limits_to_field(apps, schema_editor):
    """Lift requested amounts out of ``attributes`` into ``limits``.

    The proposal resource-request form wrote the amounts to
    ``attributes["limits"]`` and never populated the model's own ``limits``
    field. They round-tripped through the form, so they looked right, but
    ``allocate_proposal`` reads ``RequestedResource.limits`` — so an approved
    proposal provisioned a resource with no quota at all.

    Only rows whose ``limits`` is still empty are touched, so anything written
    correctly (call resource templates, imports) is left alone.
    """
    RequestedResource = apps.get_model("proposal", "RequestedResource")
    for resource in RequestedResource.objects.exclude(attributes={}).iterator():
        if resource.limits:
            continue
        legacy = (resource.attributes or {}).get("limits")
        if not isinstance(legacy, dict) or not legacy:
            continue
        resource.limits = legacy
        attributes = dict(resource.attributes)
        attributes.pop("limits", None)
        resource.attributes = attributes
        resource.save(update_fields=["limits", "attributes"])


def move_limits_back(apps, schema_editor):
    RequestedResource = apps.get_model("proposal", "RequestedResource")
    for resource in RequestedResource.objects.exclude(limits={}).iterator():
        attributes = dict(resource.attributes or {})
        attributes["limits"] = resource.limits
        resource.attributes = attributes
        resource.limits = {}
        resource.save(update_fields=["limits", "attributes"])


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0066_callworkflowstep_checklist_required"),
    ]

    operations = [
        migrations.RunPython(move_limits_to_field, move_limits_back),
    ]
