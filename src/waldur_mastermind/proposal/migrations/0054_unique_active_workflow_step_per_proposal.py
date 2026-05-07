from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0053_callworkflowstep_display_order_and_more"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="proposalworkflowstepinstance",
            constraint=models.UniqueConstraint(
                fields=["proposal"],
                condition=models.Q(status="active"),
                name="unique_active_workflow_step_per_proposal",
            ),
        ),
    ]
