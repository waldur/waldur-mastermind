from django.db import migrations


def create_call_role(apps, schema_editor):
    Call = apps.get_model("proposal", "Call")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Role = apps.get_model("permissions", "Role")

    Role.objects.create(
        name="CALL.REVIEWER",
        description="Call reviewer",
        content_type=ContentType.objects.get_for_model(Call),
        is_system_role=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0016_requestedoffering_description"),
    ]

    operations = [
        migrations.RunPython(create_call_role),
    ]
