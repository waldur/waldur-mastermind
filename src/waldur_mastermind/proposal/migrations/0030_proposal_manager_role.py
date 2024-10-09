# Generated by Django 4.2.14 on 2024-10-08 07:51

from django.db import migrations


def create_role(apps, schema_editor):
    Proposal = apps.get_model("proposal", "Proposal")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Role = apps.get_model("permissions", "Role")

    Role.objects.create(
        name="PROPOSAL.MANAGER",
        description="Proposal manager",
        content_type=ContentType.objects.get_for_model(Proposal),
        is_system_role=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0029_user_on_delete_set_null"),
    ]

    operations = [
        migrations.RunPython(create_role),
    ]
