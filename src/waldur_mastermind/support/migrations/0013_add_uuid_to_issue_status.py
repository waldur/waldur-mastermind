# Generated manually

import uuid

from django.db import migrations, models


def generate_uuids(apps, schema_editor):
    IssueStatus = apps.get_model("support", "IssueStatus")
    for issue_status in IssueStatus.objects.all():
        issue_status.uuid = uuid.uuid4()
        issue_status.save()


def reverse_uuids(apps, schema_editor):
    # No need to reverse UUID generation
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0012_alter_attachment_state_alter_comment_state_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="issuestatus",
            name="uuid",
            field=models.UUIDField(null=True, blank=True),
        ),
        migrations.RunPython(generate_uuids, reverse_uuids),
        migrations.AlterField(
            model_name="issuestatus",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False),
        ),
    ]
