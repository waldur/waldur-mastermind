from django.db import migrations, models
from modeltranslation.settings import DEFAULT_LANGUAGE
from modeltranslation.utils import build_localized_fieldname


def create_admin_role(apps, schema_editor):
    Proposal = apps.get_model("proposal", "Proposal")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Role = apps.get_model("permissions", "Role")

    # Its permissions come from permissions.yaml, loaded by import_roles.
    # Role.description is a modeltranslation field and a historical model has
    # no translation descriptors, so the served column is written explicitly.
    description = "Proposal administrator"
    Role.objects.get_or_create(
        name="PROPOSAL.ADMIN",
        defaults={
            "description": description,
            build_localized_fieldname("description", DEFAULT_LANGUAGE): description,
            "content_type": ContentType.objects.get_for_model(Proposal),
            "is_system_role": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0033_service_provider_manager_orders"),
        ("proposal", "0085_proposal_submitted_at"),
    ]

    operations = [
        migrations.RunPython(create_admin_role, migrations.RunPython.noop),
        migrations.AddField(
            model_name="callworkflowstepnotificationrule",
            name="notified_proposal_roles",
            field=models.ManyToManyField(
                blank=True,
                help_text="Only for an applicant audience: the proposal roles whose holders are notified. Empty notifies the proposal creator and every member of the proposal team.",
                related_name="+",
                to="permissions.role",
            ),
        ),
    ]
