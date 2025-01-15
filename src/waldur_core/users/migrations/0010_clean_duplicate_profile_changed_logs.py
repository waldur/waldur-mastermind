from django.db import migrations


def clean_duplicate_profile_changed_logs(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    events = Event.objects.filter(event_type="user_profile_changed")
    events.delete()


class Migration(migrations.Migration):
    dependencies = [
        (
            "users",
            "0009_alter_invitation_phone_number",
        ),
    ]

    operations = [
        migrations.RunPython(clean_duplicate_profile_changed_logs),
    ]
