from django.db import migrations


def create_role(apps, schema_editor):
    Proposal = apps.get_model("proposal", "Proposal")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Role = apps.get_model("permissions", "Role")

    Role.objects.create(
        name="PROPOSAL.MEMBER",
        description="Proposal member",
        content_type=ContentType.objects.get_for_model(Proposal),
        is_system_role=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0027_call_manager_role"),
    ]

    operations = [
        migrations.RunPython(create_role),
    ]
