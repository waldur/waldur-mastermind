from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0054_unique_active_workflow_step_per_proposal"),
    ]

    operations = [
        migrations.AlterField(
            model_name="callworkflowstep",
            name="transition_mode",
            field=models.CharField(
                choices=[
                    (
                        "automatic_on_completion",
                        "Advance automatically when step completes",
                    ),
                    ("manual", "Advance manually (call manager confirms)"),
                ],
                default="automatic_on_completion",
                help_text="How this step advances to the next.",
                max_length=32,
            ),
        ),
    ]
