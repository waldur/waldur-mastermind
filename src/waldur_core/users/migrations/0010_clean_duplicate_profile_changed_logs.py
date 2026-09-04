from django.db import migrations


def clean_duplicate_profile_changed_logs(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    events = Event.objects.filter(event_type="user_profile_changed")
    events.delete()


class Migration(migrations.Migration):
    dependencies = [
        # Reads logging.Event; logging stood at 0015 when this was written, and
        # the squash replacing it must not rely on plan order for the model.
        ("logging", "0015_event_index"),
        (
            "users",
            "0009_alter_invitation_phone_number",
        ),
    ]

    operations = [
        migrations.RunPython(clean_duplicate_profile_changed_logs),
    ]
