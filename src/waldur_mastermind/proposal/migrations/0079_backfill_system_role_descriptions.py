from django.db import migrations
from django.db.models import F, Q
from modeltranslation.settings import DEFAULT_LANGUAGE
from modeltranslation.utils import build_localized_fieldname

from waldur_core.permissions.enums import ROLE_DESCRIPTIONS


def backfill_descriptions(apps, schema_editor):
    """Give system roles whose description never reached the API a label.

    Role.description is a modeltranslation field: the API serves
    description_<default language>, not the raw description column. The
    migrations that seed roles write the raw column through a historical model,
    which has no modeltranslation descriptors, so those roles are served with an
    empty description and the UI falls back to the raw enum name.

    Only rows whose served field is blank are touched, and the existing raw
    value is promoted rather than replaced. That matters because the localized
    columns were added in 2023 (permissions/0004_translation) and nothing ever
    copied the raw column into them: a description customised before then --
    deliberately, by an operator -- sits in exactly the same state as one left
    by a seeding migration. Imposing the canonical label here would silently
    revert a site's own terminology, so the canonical value is used only when
    there is nothing to promote.

    This lives in the proposal app rather than in permissions because the call
    and proposal roles are seeded here, after every permissions migration has
    run. A backfill in permissions would execute before those rows exist.
    """
    Role = apps.get_model("permissions", "Role")
    localized_field = build_localized_fieldname("description", DEFAULT_LANGUAGE)
    # Exact name matches only, so organization-scoped clones (named
    # SCOPE.customer-slug.suffix) are never affected.
    unserved = Role.objects.filter(
        Q(**{f"{localized_field}__isnull": True}) | Q(**{localized_field: ""}),
        name__in=list(ROLE_DESCRIPTIONS),
        is_system_role=True,
    )

    # Whatever the row already says, promoted into the field the API reads.
    unserved.exclude(description="").update(**{localized_field: F("description")})

    # Only rows that say nothing at all fall back to the canonical label.
    for role_name, description in ROLE_DESCRIPTIONS.items():
        unserved.filter(name=role_name, description="").update(
            **{localized_field: description, "description": description}
        )


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0078_call_slug_index_and_default"),
        ("permissions", "0027_role_description_mk_role_description_sq"),
    ]

    operations = [
        # Deliberately not elidable: a squash that drops this would leave
        # already-affected databases unrepaired, which is how the roles lost
        # their descriptions in the first place (see marketplace 0281).
        migrations.RunPython(backfill_descriptions, migrations.RunPython.noop),
    ]
