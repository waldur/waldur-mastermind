from django.db import migrations

ALL_MODELS = ["*"]


def narrow(resource_list, key_list):
    # model_lists.narrow, as it stood when this migration was written.
    if resource_list == ALL_MODELS:
        return list(key_list)
    if key_list == ALL_MODELS:
        return list(resource_list)
    return [model for model in key_list if model in resource_list]


def key_models(resource_list, key_list, choices):
    # model_lists.key_models, as it stood when this migration was written.
    names = narrow(resource_list, key_list)
    if names == ALL_MODELS or choices is None:
        return names
    return [model for model in names if model in choices]


def mark_keys_wider_than_their_resource(apps, schema_editor):
    # Until now a resource's selection only narrowed the portal's key dialogs,
    # and a model the offering stopped listing narrowed nothing: every command
    # carried the key's own list, so that is what the backend applied. A key
    # now narrowed is marked, and the hourly send_outdated_api_key_models task
    # sends it the effective list.
    Offering = apps.get_model("marketplace", "Offering")
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    offerings = {}
    for offering in Offering.objects.filter(
        plugin_options__enable_api_key_provisioning=True
    ).only("pk", "resource_options"):
        option = ((offering.resource_options or {}).get("options") or {}).get("models")
        if not isinstance(option, dict):
            continue
        choices = option.get("choices")
        offerings[offering.pk] = (
            option.get("type") == "select_string_multi",
            choices if isinstance(choices, list) else None,
        )
    keys = (
        ResourceApiKey.objects.filter(resource__offering_id__in=list(offerings))
        .exclude(state="Deleted")
        .select_related("resource")
    )
    for api_key in keys:
        narrows, choices = offerings[api_key.resource.offering_id]
        selection = (api_key.resource.options or {}).get("models")
        if not narrows or selection is None or selection == "":
            selection = ALL_MODELS
        elif isinstance(selection, str):
            selection = [selection]
        own = api_key.allowed_models or []
        if sorted(key_models(selection, own, choices)) != sorted(own):
            api_key.models_outdated = True
            api_key.save(update_fields=["models_outdated"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0311_resource_models_all_models"),
    ]

    operations = [
        migrations.RunPython(
            mark_keys_wider_than_their_resource, migrations.RunPython.noop
        ),
    ]
