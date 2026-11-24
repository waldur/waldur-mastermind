"""Keep who has personal access tokens unchanged across the new rule.

A user used to need PAT_ENABLED and their own can_use_personal_access_tokens
flag; now either is enough. A deployment with PAT_ENABLED on had tokens for
the flagged users only, and under the new rule the same setting would give
them to every user, reviving tokens whose owner was cut off by clearing the
flag. Turning PAT_ENABLED off gives exactly the flagged users tokens, as
before. Administrators who want every user to have tokens turn it on again.

A value the deployment applies from a file on every deploy is turned off too,
but the next deploy restores it; that is logged, since only the deployment's
configuration can change it.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

KEY = "PAT_ENABLED"
SOURCE_DETAIL = "core.0062: PAT_ENABLED now gives tokens to every user"


def preserve_pat_access(apps, schema_editor):
    Setting = apps.get_model("configuration", "Setting")
    setting = Setting.objects.filter(key=KEY, value=True).first()
    if setting is None:
        return
    setting.value = False
    setting.source = "migration"
    setting.source_detail = SOURCE_DETAIL
    setting.save(update_fields=["value", "source", "source_detail", "updated_at"])
    if setting.managed_by:
        logger.warning(
            "%s was turned off so that only users with the personal access "
            "tokens flag keep their tokens, but %s sets it on every deploy. "
            "While it is on, every user may use personal access tokens; turn "
            "it off there to keep tokens for flagged users only.",
            KEY,
            setting.managed_by,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0061_user_can_use_personal_access_tokens_help_text"),
        ("configuration", "0004_drop_removed_reporting_screens"),
    ]

    operations = [
        migrations.RunPython(preserve_pat_access, migrations.RunPython.noop),
    ]
