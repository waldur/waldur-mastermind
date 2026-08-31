from django.db import migrations, models


def copy_content_from_dbtemplates(apps, schema_editor):
    """
    Copy real content overrides from the old django-dbtemplates table
    (``django_template``) into the new ``NotificationTemplate.content`` column,
    matching by name/path.

    Uses raw SQL rather than ``apps.get_model("dbtemplates", ...)`` because this
    migration ships in the same release that drops the ``dbtemplates`` app from
    INSTALLED_APPS - by then it is no longer a registered app, so its migrations
    are not part of the graph a historical app registry could be built from.

    The old load_notifications seeded django_template with the filesystem source
    for *every* template, not just overridden ones - importing that wholesale
    would freeze the shipped template at this release for every row that was
    never actually overridden, exactly the bug the load_notifications change in
    this same release fixes for fresh installs. Skip a row whose stored content
    is byte-identical to the current filesystem template: it was never a real
    override. This compares against the *current* release's shipped file, so an
    override that happens to be byte-identical to it is also skipped - a no-op
    in rendering terms either way, since the content served is the same.
    """
    if "django_template" not in schema_editor.connection.introspection.table_names():
        # Fresh install, or a deployment that never had dbtemplates installed.
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT name, content FROM django_template")
        content_by_path = dict(cursor.fetchall())

    if not content_by_path:
        return

    from waldur_core.core.template_utils import get_original_content

    NotificationTemplate = apps.get_model("core", "NotificationTemplate")
    for template in NotificationTemplate.objects.filter(path__in=content_by_path):
        stored = content_by_path[template.path]
        if stored == get_original_content(template.path):
            continue
        template.content = stored
        template.save(update_fields=["content"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0046_workflow_step_event_notification"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationtemplate",
            name="content",
            field=models.TextField(blank=True, default="", verbose_name="content"),
        ),
        migrations.RunPython(
            copy_content_from_dbtemplates, reverse_code=migrations.RunPython.noop
        ),
    ]
