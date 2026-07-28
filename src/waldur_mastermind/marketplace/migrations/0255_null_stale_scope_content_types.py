from django.apps import apps as global_apps
from django.db import migrations


def null_stale_scopes(apps, schema_editor):
    """Clear scope pointers referencing content types of removed models.

    Offerings created before their scope model was dropped from the codebase
    (e.g. support.Offering, removed in WAL-3743) keep a content_type_id whose
    model no longer exists. Read serializers mask this as scope=null, but any
    direct GenericForeignKey access (e.g. update_integration) crashes with
    AttributeError: 'NoneType' object has no attribute '_base_manager'.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")

    def is_stale(ct):
        try:
            global_apps.get_model(ct.app_label, ct.model)
        except LookupError:
            return True
        return False

    stale_ct_ids = [ct.id for ct in ContentType.objects.all() if is_stale(ct)]
    if not stale_ct_ids:
        return

    for model_name in ("Offering", "Resource"):
        model = apps.get_model("marketplace", model_name)
        model.objects.filter(content_type_id__in=stale_ct_ids).update(
            content_type=None, object_id=None
        )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0254_resourcemembersyncstatus"),
        ("marketplace", "0254_slurm_partition_qos_offering_trigger"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(null_stale_scopes, migrations.RunPython.noop),
    ]
