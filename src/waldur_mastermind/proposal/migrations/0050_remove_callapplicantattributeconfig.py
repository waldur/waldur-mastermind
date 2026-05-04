from django.db import migrations

LEGACY_EXPOSE_FIELDS = (
    "expose_full_name",
    "expose_email",
    "expose_organization",
    "expose_affiliations",
    "expose_organization_type",
    "expose_organization_country",
    "expose_nationality",
    "expose_nationalities",
    "expose_country_of_residence",
    "expose_eduperson_assurance",
    "expose_identity_source",
)


def copy_legacy_to_visibility_config(apps, schema_editor):
    """Carry forward legacy CallApplicantAttributeConfig rows into the new
    CallApplicantVisibilityConfig model before the legacy table is dropped.

    If reviewers_see_applicant_details is False on the legacy row, force every
    expose_* on the new row to False — that matches the legacy "anonymize for
    reviewers" semantics. If a CallApplicantVisibilityConfig already exists for
    the call, it is authoritative and left alone.
    """
    LegacyConfig = apps.get_model("proposal", "CallApplicantAttributeConfig")
    NewConfig = apps.get_model("proposal", "CallApplicantVisibilityConfig")

    for legacy in LegacyConfig.objects.all():
        if NewConfig.objects.filter(call=legacy.call).exists():
            continue
        if legacy.reviewers_see_applicant_details:
            data = {field: getattr(legacy, field) for field in LEGACY_EXPOSE_FIELDS}
        else:
            data = {field: False for field in LEGACY_EXPOSE_FIELDS}
        NewConfig.objects.create(call=legacy.call, **data)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0049_call_user_affiliations_call_user_assurance_levels_and_more"),
        ("proposal", "0037_callapplicantvisibilityconfig"),
    ]

    operations = [
        migrations.RunPython(copy_legacy_to_visibility_config, noop_reverse),
        migrations.DeleteModel(name="CallApplicantAttributeConfig"),
    ]
