import django.db.models.deletion
import django_fsm
from django.conf import settings
from django.db import migrations, models


def backfill_pending_action(apps, schema_editor):
    # Before this migration a key in flight could only be mid-rotation, or
    # waiting for its initial value, so the command it awaits is known.
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    ResourceApiKey.objects.filter(state="Updating").update(pending_action="rotate")
    ResourceApiKey.objects.filter(state="Creating").update(pending_action="create")
    # Until now a key's row changed only when its value did.
    ResourceApiKey.objects.exclude(key_ciphertext="").update(
        issued_at=models.F("modified")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0300_provider_project_groups"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="resourceapikey",
            unique_together=set(),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="allowed_models",
            field=models.JSONField(
                blank=True,
                help_text="Models this key may call; null allows every model.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="current_usages",
            field=models.JSONField(
                blank=True,
                help_text="Usage per component type in usage_period, as last reported by the site agent.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="usage_period",
            field=models.DateField(
                blank=True,
                help_text="The month current_usages covers (its first day). Limits are monthly: usage of an earlier month counts against none.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="paused_by_limit",
            field=models.BooleanField(
                default=False,
                help_text="Waldur paused the key because its usage reached a limit. Such a key is resumed automatically once its usage is under its limits again: in a new month, or when a limit is raised.",
            ),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="issued_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the agent last stored a value for this key: its creation or its latest rotation. Unlike modified, a pause or an edit leaves it alone.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="limits",
            field=models.JSONField(
                blank=True,
                help_text="Per-component limits, keyed by component type. Waldur pauses the key once its reported usage reaches one; zero means no limit.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="pending_action",
            field=models.CharField(
                blank=True,
                choices=[
                    ("create", "Create"),
                    ("rotate", "Rotate"),
                    ("pause", "Pause"),
                    ("resume", "Resume"),
                    ("delete", "Delete"),
                    ("update", "Update"),
                ],
                default="",
                help_text="The command the site agent is carrying out on this key. Kept when the key goes Erred, to show which command failed.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="resourceapikey",
            name="user",
            field=models.ForeignKey(
                blank=True,
                help_text="Assignee. When set, only this user can reveal the key.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="resource_api_keys",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="resourceapikey",
            name="state",
            field=django_fsm.FSMField(
                choices=[
                    ("Creating", "Creating"),
                    ("OK", "OK"),
                    ("Updating", "Updating"),
                    ("Paused", "Paused"),
                    ("Deleting", "Deleting"),
                    ("Deleted", "Deleted"),
                    ("Erred", "Erred"),
                ],
                default="Creating",
                max_length=50,
            ),
        ),
        migrations.AddConstraint(
            model_name="resourceapikey",
            constraint=models.UniqueConstraint(
                condition=models.Q(("client_id", ""), _negated=True),
                fields=("resource", "client_id"),
                name="marketplace_resource_api_key_unique_client_id",
            ),
        ),
        migrations.RunPython(backfill_pending_action, migrations.RunPython.noop),
    ]
