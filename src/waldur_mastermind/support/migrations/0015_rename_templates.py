from django.db import migrations

TEMPLATES = (
    (
        "support/notification_comment_added.html",
        "support/notification_comment_added_message.html",
    ),
    (
        "support/notification_comment_added.txt",
        "support/notification_comment_added_message.txt",
    ),
    (
        "support/notification_comment_updated.html",
        "support/notification_comment_updated_message.html",
    ),
    (
        "support/notification_comment_updated.txt",
        "support/notification_comment_updated_message.txt",
    ),
    (
        "support/notification_issue_feedback.html",
        "support/notification_issue_feedback_message.html",
    ),
    (
        "support/notification_issue_feedback.txt",
        "support/notification_issue_feedback_message.txt",
    ),
    (
        "support/notification_issue_updated.html",
        "support/notification_issue_updated_message.html",
    ),
    (
        "support/notification_issue_updated.txt",
        "support/notification_issue_updated_message.txt",
    ),
)


def rename_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("core", "NotificationTemplate")
    for old_path, new_path in TEMPLATES:
        NotificationTemplate.objects.filter(path=old_path).update(path=new_path)


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0014_alter_issuestatus_uuid"),
    ]

    operations = [
        migrations.RunPython(rename_templates),
    ]
