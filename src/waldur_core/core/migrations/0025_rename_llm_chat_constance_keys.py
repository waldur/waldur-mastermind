from django.db import migrations

RENAMES = [
    ("LLM_CHAT_ENABLED", "AI_ASSISTANT_ENABLED"),
    ("LLM_CHAT_ENABLED_ROLES", "AI_ASSISTANT_ENABLED_ROLES"),
    ("LLM_CHAT_SESSION_RETENTION_DAYS", "AI_ASSISTANT_SESSION_RETENTION_DAYS"),
    ("LLM_CHAT_HISTORY_LIMIT", "AI_ASSISTANT_HISTORY_LIMIT"),
    ("LLM_INFERENCES_BACKEND_TYPE", "AI_ASSISTANT_BACKEND_TYPE"),
    ("LLM_INFERENCES_API_URL", "AI_ASSISTANT_API_URL"),
    ("LLM_INFERENCES_API_TOKEN", "AI_ASSISTANT_API_TOKEN"),
    ("LLM_INFERENCES_MODEL", "AI_ASSISTANT_MODEL"),
    ("LLM_COMPLETION_KWARGS", "AI_ASSISTANT_COMPLETION_KWARGS"),
    ("LLM_TOKEN_LIMIT_DAILY", "AI_ASSISTANT_TOKEN_LIMIT_DAILY"),
    ("LLM_TOKEN_LIMIT_WEEKLY", "AI_ASSISTANT_TOKEN_LIMIT_WEEKLY"),
    ("LLM_TOKEN_LIMIT_MONTHLY", "AI_ASSISTANT_TOKEN_LIMIT_MONTHLY"),
    ("LLM_INJECTION_ALLOWLIST", "AI_ASSISTANT_INJECTION_ALLOWLIST"),
]


def rename_forward(apps, schema_editor):
    for old_key, new_key in RENAMES:
        # Delete any pre-existing new key to avoid unique constraint violation
        # (constance may auto-create keys before the migration runs)
        schema_editor.execute(
            "DELETE FROM constance_constance WHERE key = %s",
            [new_key],
        )
        schema_editor.execute(
            "UPDATE constance_constance SET key = %s WHERE key = %s",
            [new_key, old_key],
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0024_remove_constance_site_logo"),
    ]

    operations = [
        migrations.RunPython(rename_forward, migrations.RunPython.noop),
    ]
