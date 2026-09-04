"""Bring the ``proposal_call.slug`` column in line with the model.

``0036_call_slug`` added the column with raw SQL - a ``DEFAULT ''`` Django never
drops - and only told the migration state about the field, so the index a
``SlugField`` declares was never created. Every database that came through that
migration carries the default and lacks the index; one initialised from the models
(``migrate_fresh``, or a squash that folded the field into ``CreateModel``) has
neither problem. Idempotent: the index is created only when its name is absent.
"""

from django.db import migrations


def align_call_slug(apps, schema_editor):
    Call = apps.get_model("proposal", "Call")
    table = Call._meta.db_table
    schema_editor.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN "slug" DROP DEFAULT', params=None
    )
    # Statement names render quoted ("proposal_call_slug_a408e9e9"); introspection
    # returns them bare. Compare like with like or the index is created twice.
    with schema_editor.connection.cursor() as cursor:
        existing = {
            schema_editor.quote_name(name)
            for name in schema_editor.connection.introspection.get_constraints(
                cursor, table
            )
        }
    for statement in schema_editor._field_indexes_sql(
        Call, Call._meta.get_field("slug")
    ):
        if str(statement.parts["name"]) not in existing:
            schema_editor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0077_workflow_notification_rules_and_panel_chair"),
    ]

    operations = [
        migrations.RunPython(align_call_slug, migrations.RunPython.noop),
    ]
