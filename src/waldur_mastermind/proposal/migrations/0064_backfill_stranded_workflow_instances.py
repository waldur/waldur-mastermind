from django.db import migrations

from waldur_mastermind.proposal.migrations._backfill_stranded_workflow_instances import (
    backfill_stranded_workflow_instances,
)


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0063_alter_callworkflowstep_min_score_threshold_and_more"),
    ]

    operations = [
        migrations.RunPython(
            backfill_stranded_workflow_instances,
            migrations.RunPython.noop,
        ),
    ]
