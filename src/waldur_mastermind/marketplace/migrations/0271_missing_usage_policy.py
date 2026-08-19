from django.db import migrations, models


def set_policy_from_recurring(apps, schema_editor):
    ComponentUsage = apps.get_model("marketplace", "ComponentUsage")
    ComponentUsage.objects.filter(recurring=True).update(missing_usage_policy="reuse")


def set_recurring_from_policy(apps, schema_editor):
    ComponentUsage = apps.get_model("marketplace", "ComponentUsage")
    ComponentUsage.objects.filter(missing_usage_policy="reuse").update(recurring=True)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0270_scrub_secret_options_from_reversion"),
    ]

    operations = [
        migrations.AddField(
            model_name="componentusage",
            name="missing_usage_policy",
            field=models.CharField(
                choices=[
                    ("none", "Leave the period unreported."),
                    ("reuse", "Reuse the reported value every month until changed."),
                    ("zero", "Record zero when no usage is reported."),
                ],
                default="none",
                help_text="What to record when no usage is reported for the following billing period.",
                max_length=10,
            ),
        ),
        migrations.RunPython(
            set_policy_from_recurring,
            set_recurring_from_policy,
            elidable=True,
        ),
        migrations.RemoveField(
            model_name="componentusage",
            name="recurring",
        ),
    ]
