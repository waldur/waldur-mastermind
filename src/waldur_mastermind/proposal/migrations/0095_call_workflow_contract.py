"""The call workflow, part 3 of 3: what holds once 0094 has filled it in.

Every call step and step instance now has its order and its row, so they
become required and unique; the proposal's old step key and the imported
reviewer statistics are dropped.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0094_call_workflow_backfill"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="proposal",
            name="workflow_step",
        ),
        migrations.AlterField(
            model_name="callworkflowstep",
            name="order",
            field=models.PositiveIntegerField(
                help_text="Position of the step in the call's workflow."
            ),
        ),
        migrations.AlterField(
            model_name="proposalworkflowstepinstance",
            name="call_step",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="proposal_instances",
                to="proposal.callworkflowstep",
            ),
        ),
        migrations.AlterField(
            model_name="proposalworkflowstepinstance",
            name="order",
            field=models.PositiveIntegerField(
                help_text="Position of the step in this proposal's path."
            ),
        ),
        migrations.AddConstraint(
            model_name="callworkflowstep",
            constraint=models.UniqueConstraint(
                fields=("call", "key"), name="proposal_unique_call_step_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="callworkflowstep",
            constraint=models.UniqueConstraint(
                fields=("call", "order"), name="proposal_unique_call_step_order"
            ),
        ),
        migrations.AddConstraint(
            model_name="proposalworkflowstepinstance",
            constraint=models.UniqueConstraint(
                condition=models.Q(("run__isnull", True)),
                fields=("proposal", "call_step"),
                name="proposal_unique_step_instance_per_call_step",
            ),
        ),
        migrations.AddConstraint(
            model_name="proposalworkflowstepinstance",
            constraint=models.UniqueConstraint(
                condition=models.Q(("run__isnull", False)),
                fields=("run", "call_step"),
                name="proposal_unique_step_instance_per_run_step",
            ),
        ),
        migrations.DeleteModel(
            name="ReviewerStats",
        ),
    ]
