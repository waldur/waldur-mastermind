from django.db import migrations


def delete_pending_requests(apps, schema_editor):
    # Requests opened before an email change needed re-authentication could
    # have been opened with a stolen API token alone, and confirm_email would
    # still apply them. Users ask again, this time confirming who they are.
    apps.get_model("core", "ChangeEmailRequest").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0056_reauthentication"),
    ]

    operations = [
        migrations.RunPython(delete_pending_requests, migrations.RunPython.noop),
    ]
