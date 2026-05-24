from django.db import migrations


def backfill_allocation_decision(apps, schema_editor):
    """Seed allocation_decision workflow step for existing draft calls only.

    Active/archived calls are already running with their configured steps —
    leaving them untouched avoids surprising in-flight workflows.
    """
    Call = apps.get_model("proposal", "Call")
    CallWorkflowStep = apps.get_model("proposal", "CallWorkflowStep")

    for call in Call.objects.filter(state="draft"):
        CallWorkflowStep.objects.get_or_create(
            call=call,
            step="allocation_decision",
            defaults={
                "is_enabled": True,
                "responsible_role": "call_manager",
                "transition_mode": "automatic_on_completion",
            },
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("proposal", "0055_alter_callworkflowstep_transition_mode"),
    ]

    operations = [
        migrations.RunPython(backfill_allocation_decision, noop),
    ]
