from django.db import migrations, models


def backfill_pending_action(apps, schema_editor):
    # Before 0301 a key in flight could only be mid-rotation, or waiting for
    # its initial value, so the command it awaits is known.
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    ResourceApiKey.objects.filter(state="Updating", pending_action="").update(
        pending_action="rotate"
    )
    ResourceApiKey.objects.filter(state="Creating", pending_action="").update(
        pending_action="create"
    )
    # Until now a key's row changed only when its value did.
    ResourceApiKey.objects.exclude(key_ciphertext="").filter(
        issued_at__isnull=True
    ).update(issued_at=models.F("modified"))


class Migration(migrations.Migration):
    # Kept apart from 0301: these UPDATEs queue deferred FK trigger events, and
    # Postgres then refuses the index 0301 creates in the same transaction.
    dependencies = [
        ("marketplace", "0301_resource_api_key_management"),
    ]

    operations = [
        migrations.RunPython(backfill_pending_action, migrations.RunPython.noop),
    ]
