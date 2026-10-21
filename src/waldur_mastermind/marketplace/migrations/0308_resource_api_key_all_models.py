from django.db import migrations


def null_to_all_models(apps, schema_editor):
    # Null used to mean every model. It is now spelled out, so that an empty
    # list can mean none: existing keys keep calling every model.
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    ResourceApiKey.objects.filter(allowed_models__isnull=True).update(
        allowed_models=["*"]
    )


def all_models_to_null(apps, schema_editor):
    # The code this goes back to cannot say "no model": it reads an empty list
    # as every model, like null. Rolling back over a key that may call no model
    # would quietly let it call every one, so refuse and leave the choice of
    # what such a key should become to the operator.
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    no_models = ResourceApiKey.objects.filter(allowed_models=[])
    if no_models.exists():
        uuids = ", ".join(sorted(key.uuid.hex for key in no_models))
        raise RuntimeError(
            "Cannot reverse: these resource API keys may call no model, which "
            "the earlier code would read as every model: "
            f"{uuids}. Give each a model list or delete it, then re-run the "
            "rollback."
        )
    ResourceApiKey.objects.filter(allowed_models=["*"]).update(allowed_models=None)


class Migration(migrations.Migration):
    # Kept apart from 0309, which makes the column non-null: on Postgres a data
    # change and a schema change of the same table do not share a migration.
    dependencies = [
        (
            "marketplace",
            "0307_category_description_en_gb_category_title_en_gb_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(null_to_all_models, all_models_to_null),
    ]
