from django.db import migrations

ALL_MODELS = ["*"]


def selection_resources(apps):
    # A multiple-choice models option is a model selection on any offering
    # (model_lists.models_option), so that one turning on key management later
    # finds its resources on every model.
    Offering = apps.get_model("marketplace", "Offering")
    Resource = apps.get_model("marketplace", "Resource")
    offering_ids = [
        offering.pk
        for offering in Offering.objects.filter(
            resource_options__options__has_key="models"
        ).only("pk", "resource_options")
        if (
            ((offering.resource_options or {}).get("options") or {}).get("models") or {}
        ).get("type")
        == "select_string_multi"
    ]
    return Resource.objects.filter(offering_id__in=offering_ids)


def empty_to_all_models(apps, schema_editor):
    # An empty or missing selection used to leave a resource's keys free to call
    # every model. Empty now means none, so every model is spelled out and
    # existing resources keep it.
    for resource in selection_resources(apps).only("pk", "options"):
        options = resource.options or {}
        if options.get("models"):
            continue
        resource.options = {**options, "models": ALL_MODELS}
        resource.save(update_fields=["options"])


def all_models_to_empty(apps, schema_editor):
    # The code this goes back to reads an empty selection as every model.
    for resource in selection_resources(apps).only("pk", "options"):
        options = resource.options or {}
        if options.get("models") == ALL_MODELS:
            resource.options = {**options, "models": []}
            resource.save(update_fields=["options"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0310_resource_api_key_models_outdated"),
    ]

    operations = [
        migrations.RunPython(empty_to_all_models, all_models_to_empty),
    ]
