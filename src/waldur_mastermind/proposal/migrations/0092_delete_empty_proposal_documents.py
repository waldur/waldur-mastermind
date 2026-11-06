from django.db import migrations
from django.db.models import Q


def delete_empty_documents(apps, schema_editor):
    # A document row without a file has nothing to show or download, and its
    # size cannot be read, so it only ever broke reading its proposal.
    ProposalDocumentation = apps.get_model("proposal", "ProposalDocumentation")
    ProposalDocumentation.objects.filter(Q(file__isnull=True) | Q(file="")).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0091_proposal_project_summary_max_length"),
    ]

    operations = [
        migrations.RunPython(delete_empty_documents, migrations.RunPython.noop),
    ]
